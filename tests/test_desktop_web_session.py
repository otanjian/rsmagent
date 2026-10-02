# encoding:utf-8
"""Desktop native-parent -> Web-child session bootstrap (change tasks 3.1-3.8).

The property this file exists to protect is *independence with a leash*: the
Web page the desktop carries must not hold (or be able to ask for) the native
Bearer, so the server mints a second, separate session and delivers its secret
only through a host-only HttpOnly Cookie -- while every later request through
the ordinary identity seam still re-checks that the native parent is alive.

Everything here is asserted against the real identity database and the real
WSGI app built by :class:`WebAppHarness`; the native parent is minted through
the genuine PKCE exchange so the ``desktop_native_origins`` stamp (task 3.2) is
exercised rather than simulated.
"""

import json
import os
import tempfile
import threading
import unittest

from tests._helpers import WebAppHarness, cookie_value

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43  # 43 chars of the allowed set -- a real S256 verifier
BOOTSTRAP_ID = "b" * 22  # 22 base64url chars == 132 bits, the documented floor
INSTANCE_ID = "i" * 22


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    """Stand-in for a phase-1 capability that has passed its gates."""

    enabled = True

    def is_open(self, action):
        return True


class _DesktopIdentityBase(unittest.TestCase):
    """One harness (real identity DB + real app) per class."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-web-session-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.app.member("desktop-u1", ["member"])
        cls.app.member("desktop-u2", ["member"])

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    # -- native parent minting --------------------------------------------

    def native_session(self, username="desktop-u1", origin=None,
                       session_token=None):
        """Mint a genuine native session through the PKCE flow.

        ``origin`` is only used by service-level tests; the HTTP path stamps
        whatever origin the token request was actually served at.
        """
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = session_token or self.app.login(username)
        started = desktop.begin(
            session_token=web_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=web_token)
        if origin is None:
            return desktop.exchange(
                code=confirmed["code"], verifier=VERIFIER,
                client_id=CLIENT_ID, redirect_uri=REDIRECT_URI)["token"]
        return desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=origin)["token"]

    def web_service(self):
        from auth.desktop_web_session import service_for
        return service_for(self.app.service)

    def _web_bearer(self):
        """A browser-style login Bearer (no desktop mint, no origin stamp)."""
        return self.app.login("desktop-u1")


class DesktopWebSessionServiceTests(_DesktopIdentityBase):
    """Tasks 3.1/3.5/3.6 at the service seam, where 3.7's caller sits."""

    def test_bootstrap_mints_an_independent_child(self):
        native = self.native_session(origin="https://console.test")
        result = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1, origin="https://console.test")
        child = result["web_token"]
        self.assertNotEqual(child, native)
        # The child is a *separate* live session for the same user...
        verified = self.app.service.verify_session(child)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user"]["username"], "desktop-u1")
        # ...and the parent is untouched.
        self.assertIsNotNone(self.app.service.verify_session(native))

    def test_child_stops_when_parent_is_revoked(self):
        """Task 3.5: the leash is re-checked on every identity resolution."""
        native = self.native_session(origin="https://console.test")
        child = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1,
            origin="https://console.test")["web_token"]
        self.assertIsNotNone(self.app.service.verify_session(child))
        self.app.service.revoke_session(native)
        self.assertIsNone(self.app.service.verify_session(child))

    def test_child_revocation_tears_down_the_parent(self):
        """Task 3.6: logging out of the page ends the desktop session too."""
        native = self.native_session(origin="https://console.test")
        child = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1,
            origin="https://console.test")["web_token"]
        self.app.service.revoke_session(child)
        self.assertIsNone(self.app.service.verify_session(native))
        self.assertIsNone(self.app.service.verify_session(child))

    def test_an_independent_browser_session_survives_the_pair_logout(self):
        """Only the pair dies: an unrelated browser login of the same user stays."""
        native = self.native_session(origin="https://console.test")
        child = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1,
            origin="https://console.test")["web_token"]
        unrelated = self.app.login("desktop-u1")
        self.app.service.revoke_session(child)
        self.assertIsNone(self.app.service.verify_session(native))
        self.assertIsNotNone(self.app.service.verify_session(unrelated))

    def test_password_change_revokes_the_pair(self):
        """Task 3.6: 改密 reaches the pairing through the ordinary seams."""
        previous = self.app.service.create_member(
            actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
            operation="create-new", username="desktop-pw",
            display_name="Desktop Pw", temporary_password="TempPass123!",
            roles=["member"])
        self.assertTrue(previous["user_id"])
        self.app.service.change_password(
            self.app.service.login("desktop-pw", "TempPass123!").token,
            "TempPass123!", "MemberPass456!")
        live = self.app.service.login("desktop-pw", "MemberPass456!")
        native = self.native_session(origin="https://console.test",
                                    session_token=live.token)
        child = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1,
            origin="https://console.test")["web_token"]
        self.app.service.change_password(live.token, "MemberPass456!",
                                        "MemberPass789!")
        self.assertIsNone(self.app.service.verify_session(child))
        self.assertIsNone(self.app.service.verify_session(native))

    def test_a_plain_login_token_cannot_bootstrap(self):
        """Task 3.1: no origin registration -- the source cannot be re-verified."""
        from auth.desktop_web_session import DesktopWebSessionError
        with self.assertRaises(DesktopWebSessionError) as caught:
            self.web_service().bootstrap(
                native_token=self._web_bearer(), bootstrap_id=BOOTSTRAP_ID,
                instance_id=INSTANCE_ID, web_protocol=1,
                origin="http://localhost:9899")
        self.assertEqual(caught.exception.code, "unsupported_origin")
        self.assertEqual(caught.exception.status, 400)

    def test_origin_mismatch_is_refused(self):
        from auth.desktop_web_session import DesktopWebSessionError
        native = self.native_session(origin="https://console.test")
        with self.assertRaises(DesktopWebSessionError) as caught:
            self.web_service().bootstrap(
                native_token=native, bootstrap_id=BOOTSTRAP_ID,
                instance_id=INSTANCE_ID, web_protocol=1,
                origin="https://evil.test")
        self.assertEqual(caught.exception.code, "unsupported_origin")

    def test_replaying_a_bootstrap_id_is_refused(self):
        from auth.desktop_web_session import DesktopWebSessionError
        native = self.native_session(origin="https://console.test")
        first = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1, origin="https://console.test")
        with self.assertRaises(DesktopWebSessionError) as caught:
            self.web_service().bootstrap(
                native_token=native, bootstrap_id=BOOTSTRAP_ID,
                instance_id=INSTANCE_ID, web_protocol=1,
                origin="https://console.test")
        self.assertEqual(caught.exception.code, "bootstrap_consumed")
        self.assertEqual(caught.exception.status, 409)
        # The first child still works -- a replay must not be able to kill it.
        self.assertIsNotNone(
            self.app.service.verify_session(first["web_token"]))

    def test_a_new_bootstrap_id_replaces_the_previous_child(self):
        native = self.native_session(origin="https://console.test")
        service = self.web_service()
        first = service.bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1, origin="https://console.test")
        second = service.bootstrap(
            native_token=native, bootstrap_id="c" * 22,
            instance_id=INSTANCE_ID, web_protocol=1, origin="https://console.test")
        self.assertIsNone(self.app.service.verify_session(first["web_token"]))
        self.assertIsNotNone(self.app.service.verify_session(second["web_token"]))
        self.assertEqual(service.status(native_token=native)["link_id"],
                         second["link_id"])

    def test_concurrent_bootstraps_leave_one_live_child(self):
        """The partial unique index -- not a check-then-insert -- is the guard."""
        native = self.native_session(origin="https://console.test")
        service = self.web_service()
        results, errors = [], []

        def run(bootstrap_id):
            try:
                results.append(service.bootstrap(
                    native_token=native, bootstrap_id=bootstrap_id,
                    instance_id=INSTANCE_ID, web_protocol=1,
                    origin="https://console.test"))
            except Exception as exc:  # noqa: BLE001 - recorded and asserted
                errors.append(exc)

        threads = [threading.Thread(target=run, args=("d" * 22,)),
                   threading.Thread(target=run, args=("e" * 22,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        live = self.app.service._store.execute(
            "SELECT id FROM desktop_web_links"
            " WHERE bootstrap_id IN (?,?) AND revoked_at IS NULL",
            ("d" * 22, "e" * 22))
        self.assertEqual(len(live), 1)
        # Exactly one child the server still honours, whatever the race decided.
        survivors = [r["web_token"] for r in results
                     if self.app.service.verify_session(r["web_token"])]
        self.assertEqual(len(survivors), 1, errors)

    def test_a_bad_instance_id_creates_nothing(self):
        from auth.desktop_web_session import DesktopWebSessionError
        native = self.native_session(origin="https://console.test")
        before = len(self.app.service._store.execute(
            "SELECT id FROM desktop_web_links WHERE bootstrap_id=?",
            ("f" * 22,)))
        with self.assertRaises(DesktopWebSessionError) as caught:
            self.web_service().bootstrap(
                native_token=native, bootstrap_id="f" * 22,
                instance_id="short", web_protocol=1, origin="https://console.test")
        self.assertEqual(caught.exception.code, "invalid_request")
        after = len(self.app.service._store.execute(
            "SELECT id FROM desktop_web_links WHERE bootstrap_id=?",
            ("f" * 22,)))
        self.assertEqual(before, after)
        self.assertEqual(self.web_service().status(
            native_token=native)["state"], "none")

    def test_protocol_mismatch_is_refused(self):
        from auth.desktop_web_session import DesktopWebSessionError
        native = self.native_session(origin="https://console.test")
        for bogus in (2, None, "1"):
            with self.assertRaises(DesktopWebSessionError) as caught:
                self.web_service().bootstrap(
                    native_token=native, bootstrap_id=BOOTSTRAP_ID,
                    instance_id=INSTANCE_ID, web_protocol=bogus,
                    origin="https://console.test")
            self.assertEqual(caught.exception.code, "protocol_mismatch")

    def test_a_restricted_session_cannot_bootstrap(self):
        """A temporary-password session may hold nothing, not even a child."""
        from auth.desktop_web_session import DesktopWebSessionError
        created = self.app.service.create_member(
            actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
            operation="create-new", username="desktop-temp",
            display_name="Desktop Temp", temporary_password="TempPass123!",
            roles=["member"])
        restricted = self.app.service.login("desktop-temp", "TempPass123!")
        self.assertIsNotNone(created["user_id"])
        with self.assertRaises(DesktopWebSessionError) as caught:
            self.web_service().bootstrap(
                native_token=restricted.token, bootstrap_id=BOOTSTRAP_ID,
                instance_id=INSTANCE_ID, web_protocol=1,
                origin="http://localhost:9899")
        self.assertEqual(caught.exception.code, "password_change_required")
        self.assertEqual(caught.exception.status, 403)

    def test_unauthenticated_bootstrap_is_a_401(self):
        from auth.desktop_web_session import DesktopWebSessionError
        with self.assertRaises(DesktopWebSessionError) as caught:
            self.web_service().bootstrap(
                native_token="", bootstrap_id=BOOTSTRAP_ID,
                instance_id=INSTANCE_ID, web_protocol=1,
                origin="https://console.test")
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertEqual(caught.exception.status, 401)

    def test_every_refusal_matches_the_frozen_contract(self):
        """Code -> HTTP status is shared with the desktop client; never guess it."""
        from auth import desktop_contracts
        from auth.desktop_web_session import DesktopWebSessionError

        def refusal(**kwargs):
            with self.assertRaises(DesktopWebSessionError) as caught:
                self.web_service().bootstrap(**kwargs)
            return caught.exception

        native = self.native_session(origin="https://console.test")
        base = dict(native_token=native, bootstrap_id=BOOTSTRAP_ID,
                    instance_id=INSTANCE_ID, web_protocol=1,
                    origin="https://console.test")
        self.web_service().bootstrap(**base)  # consume BOOTSTRAP_ID

        refusals = [
            refusal(**dict(base, web_protocol=2)),
            refusal(**dict(base, bootstrap_id="x")),
            refusal(**dict(base, origin="https://other.test")),
        ]
        for error in refusals:
            self.assertEqual(desktop_contracts.status_for(error.code),
                             error.status, error.code)
        replay = refusal(**base)
        self.assertEqual(replay.code, "bootstrap_consumed")
        self.assertEqual(desktop_contracts.status_for(replay.code), replay.status)

    def test_status_never_exposes_a_secret(self):
        native = self.native_session(origin="https://console.test")
        result = self.web_service().bootstrap(
            native_token=native, bootstrap_id=BOOTSTRAP_ID,
            instance_id=INSTANCE_ID, web_protocol=1, origin="https://console.test")
        status = self.web_service().status(native_token=native)
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["link_id"], result["link_id"])
        blob = json.dumps(status)
        self.assertNotIn(result["web_token"], blob)
        self.assertNotIn(native, blob)


class DesktopWebSessionWireTests(_DesktopIdentityBase):
    """Tasks 3.3/3.4 over the real app: Bearer-only, cookie-delivered."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from unittest.mock import patch
        cls._patch = patch("auth.capability_matrix.slice_for",
                           return_value=_EnabledSlice())
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        super().tearDownClass()

    def _native_bearer_over_the_wire(self, username="desktop-u1"):
        """The real token endpoint, so the origin stamp matches this Host."""
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        session_token = self.app.login(username)
        started = desktop.begin(
            session_token=session_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=session_token)
        response = self.app.post("/auth/desktop/token", {
            "grant_type": "authorization_code", "code": confirmed["code"],
            "code_verifier": VERIFIER, "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
        }, token=None)
        self.assertEqual(response.status.split()[0], "200", response.status)
        return WebAppHarness.json(response)["token"]

    def _bootstrap(self, native, body=None, headers=None):
        payload = {"bootstrap_id": BOOTSTRAP_ID, "instance_id": INSTANCE_ID,
                   "web_protocol": 1}
        payload.update(body or {})
        merged = {"Authorization": "Bearer " + native}
        merged.update(headers or {})
        return self.app.post("/auth/desktop/web-session", payload, token=None,
                             headers=merged)

    def test_bootstrap_delivers_the_secret_only_as_a_cookie(self):
        native = self._native_bearer_over_the_wire()
        response = self._bootstrap(native)
        self.assertEqual(response.status.split()[0], "200", response.status)
        raw = ""
        for name, value in getattr(response, "header_items", None) or []:
            if name == "Set-Cookie":
                raw = value
        self.assertIn("cow_session=", raw)
        self.assertIn("HttpOnly", raw)
        self.assertIn("Secure", raw)
        self.assertIn("SameSite=Lax", raw)
        self.assertIn("Path=/", raw)
        self.assertNotIn("Domain=", raw)  # host-only, never a shared cookie

        body = WebAppHarness.json(response)["data"]
        child = raw.split("cow_session=", 1)[1].split(";", 1)[0]
        self.assertNotEqual(child, native)
        blob = json.dumps(body)
        self.assertNotIn(child, blob)
        self.assertNotIn(native, blob)
        self.assertNotIn("web_token", body)

        # The Cookie the browser got is a working session on its own.
        self.assertIsNotNone(self.app.service.verify_session(child))

    def test_the_native_logout_revokes_the_paired_child(self):
        """Task 3.6 over the wire: the desktop sign-out ends the pairing.

        The desktop broker signs out by POSTing ``/auth/logout`` with the
        native Bearer -- the same token it bootstrapped the child with. That
        call must revoke the parent session *and* the links it parents, or the
        container is torn down locally while the server keeps the pairing live.
        ``test_child_stops_when_parent_is_revoked`` drives the service seam;
        this drives the HTTP route the shipped client actually calls.
        """
        native = self._native_bearer_over_the_wire()
        response = self._bootstrap(native)
        self.assertEqual(response.status.split()[0], "200", response.status)
        link_id = WebAppHarness.json(response)["data"]["link_id"]

        logout = self.app.request(
            "/auth/logout", "POST", None, token=None, tenant=False,
            headers={"Authorization": "Bearer " + native})
        self.assertEqual(logout.status.split()[0], "200", logout.status)

        rows = self.app.service._store.execute(
            "SELECT revoked_at FROM desktop_web_links WHERE id=?", (link_id,))
        self.assertTrue(rows, "the bootstrap must have stored a link")
        self.assertIsNotNone(rows[0]["revoked_at"],
                             "the native logout must revoke its paired Web child")

    def test_a_paired_sign_out_leaves_the_host_reading_the_session_not_the_status(self):
        """The two ends of a container sign-out, in the order the console runs them.

        ``handleLogout``/``desktopRelogin`` end the page's (child) session first,
        then call the host's ``signOut``, whose last step is ``auth-broker.logout``:
        ``POST /auth/logout`` with the *native* Bearer. In the paired state the
        page's own call has already revoked the native parent (that is what
        revoking a pair means), so this second call arrives with a Bearer the
        identity store no longer knows.

        ``DbAuthLogoutHandler`` gates every write through ``_csrf_ok()``, and for a
        Bearer source it asks whether that credential authenticates. A revoked
        Bearer therefore fails the *CSRF* check rather than the *session* check,
        and the route answers ``403 cross_origin``. A client that reads only the
        status cannot tell that apart from "the server refused to revoke a live
        session", and ``auth-broker.logout`` treated anything but 200/401 as
        unconfirmed: it set ``blockedReason='logout_incomplete'`` and froze
        business traffic. That was the real Electron journey -- the shell showed
        "退出未完成 ... 请重试退出" after a sign-out the server had in fact completed.

        What the host therefore needs from the wire, and what this pins: after the
        page's sign-out, the session is *provably* gone through a read that takes
        the credential alone. That read is the recovery the broker performs when
        the status is not a confirmation, so it must answer "not signed in" for a
        session that has already ended.
        """
        native = self._native_bearer_over_the_wire()
        boot = self._bootstrap(native)
        child = cookie_value(boot, "cow_session")
        self.assertTrue(child, "the bootstrap must deliver the child secret as a Cookie")

        # 1. The page ends its own session (console.js: endWebSession).
        page = self.app.post("/auth/logout", None, token=child)
        self.assertEqual(page.status.split()[0], "200", page.status)

        # 2. The host ends the native session (auth-broker.logout).
        host = self.app.post(
            "/auth/logout", None, token=None,
            headers={"Authorization": "Bearer " + native})
        host_status = host.status.split()[0]
        if host_status not in ("200", "401"):
            # Not a confirmation: the only legitimate answer here is the CSRF gate
            # reporting the dead credential as an origin problem. A different
            # refusal means the sign-out failed for a reason the host may not
            # treat as "already ended".
            self.assertEqual(
                (host_status, self.app.json(host)["code"]), ("403", "cross_origin"),
                f"the host's sign-out was refused for an unexpected reason: "
                f"{host.status} {host.data!r}")

        # 3. The read the host falls back on, and the fact that makes the fallback
        #    sound: the session it was asked to end is gone.
        me = self.app.get("/auth/me", token=None,
                          headers={"Authorization": "Bearer " + native})
        self.assertEqual(me.status.split()[0], "401",
                         f"a revoked native session must not read as live: {me.status} {me.data!r}")

    def test_bootstrap_body_carries_the_link_deadline_the_client_requires(self):
        """Task 3.7: the child secret is delivered *only* by Cookie -- but the
        link id, the deadline and the protocol major are body fields, and the
        desktop client refuses an answer without them (``remote/web-session.ts``).
        Removing the deadline here once broke every container attach with
        "the bootstrap answer carried no expiry"."""
        import time
        native = self._native_bearer_over_the_wire()
        response = self._bootstrap(native)
        self.assertEqual(response.status.split()[0], "200", response.status)
        data = WebAppHarness.json(response)["data"]
        self.assertIsInstance(data.get("link_id"), str)
        self.assertTrue(data["link_id"])
        self.assertEqual(data.get("web_protocol"), 1)
        self.assertNotIn("web_token", data)
        expires_at = data.get("expires_at")
        self.assertIsInstance(expires_at, int)
        self.assertGreater(expires_at, int(time.time()))
        # The Cookie's lifetime is that same deadline, not a second opinion.
        raw = ""
        for name, value in getattr(response, "header_items", None) or []:
            if name == "Set-Cookie":
                raw = value
        max_age = int(raw.split("Max-Age=", 1)[1].split(";", 1)[0])
        self.assertGreater(max_age, 0)
        self.assertLessEqual(abs(max_age - (expires_at - int(time.time()))), 2)

    def test_the_response_is_never_cached(self):
        native = self._native_bearer_over_the_wire()
        response = self._bootstrap(native)
        cache = ""
        for name, value in getattr(response, "header_items", None) or []:
            if name.lower() == "cache-control":
                cache = value
        self.assertIn("no-store", cache)

    def test_a_cookie_cannot_stand_in_for_the_native_bearer(self):
        token = self.app.login("desktop-u1")
        response = self.app.post("/auth/desktop/web-session", {
            "bootstrap_id": BOOTSTRAP_ID, "instance_id": INSTANCE_ID,
            "web_protocol": 1}, token=token)
        self.assertEqual(response.status.split()[0], "401", response.status)
        self.assertEqual(WebAppHarness.json(response)["code"], "auth_required")

    def test_a_plain_login_bearer_is_refused(self):
        response = self.app.post("/auth/desktop/web-session", {
            "bootstrap_id": BOOTSTRAP_ID, "instance_id": INSTANCE_ID,
            "web_protocol": 1}, token=None,
            headers={"Authorization": "Bearer " + self._web_bearer()})
        self.assertEqual(response.status.split()[0], "400", response.status)
        self.assertEqual(WebAppHarness.json(response)["code"],
                         "unsupported_origin")

    def test_status_reports_the_link_without_secrets(self):
        native = self._native_bearer_over_the_wire()
        self._bootstrap(native)
        response = self.app.get(
            "/auth/desktop/web-session", token=None,
            headers={"Authorization": "Bearer " + native})
        self.assertEqual(response.status.split()[0], "200", response.status)
        data = WebAppHarness.json(response)["data"]
        self.assertEqual(data["state"], "active")
        self.assertNotIn("web_token", data)
        self.assertNotIn(native, json.dumps(data))

    def test_the_bootstrap_id_is_single_use_on_the_wire(self):
        native = self._native_bearer_over_the_wire("desktop-u2")
        self.assertEqual(self._bootstrap(native).status.split()[0], "200")
        replay = self._bootstrap(native)
        self.assertEqual(replay.status.split()[0], "409", replay.status)
        self.assertEqual(WebAppHarness.json(replay)["code"],
                         "bootstrap_consumed")


class DesktopWebSessionGateTests(_DesktopIdentityBase):
    """The capability gate: closed means a reason, not a half-open surface."""

    def test_closed_capability_answers_feature_unavailable(self):
        from auth import capability_matrix
        from unittest.mock import patch

        native = self._native_bearer_over_the_wire_for_gate()
        closed = type("Closed", (), {"enabled": False})()
        with patch.object(capability_matrix, "slice_for", return_value=closed):
            response = self.app.post("/auth/desktop/web-session", {
                "bootstrap_id": BOOTSTRAP_ID, "instance_id": INSTANCE_ID,
                "web_protocol": 1}, token=None,
                headers={"Authorization": "Bearer " + native})
        self.assertEqual(response.status.split()[0], "503", response.status)
        self.assertEqual(WebAppHarness.json(response)["code"],
                         "feature_unavailable")

    def _native_bearer_over_the_wire_for_gate(self):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        session_token = self.app.login("desktop-u1")
        started = desktop.begin(
            session_token=session_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=session_token)
        return desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=self.app.BASE)["token"]

    def test_the_route_is_declared_personal(self):
        from channel.web.route_registry import derive_route_policy
        policy = derive_route_policy()["/auth/desktop/web-session"]
        self.assertEqual(policy["POST"]["policy"], "personal")
        self.assertEqual(policy["GET"]["policy"], "personal")


class DesktopWebSessionCookieContractTests(unittest.TestCase):
    """The cookie the server sets and the one the client accepts are one contract.

    ``contracts/desktop/v1.json`` ``web_session`` is declared once and read by
    both languages; these tests pin the Python side to it, and
    ``tests/test_desktop_web_session_bridge.cjs`` pins the desktop side.
    """

    def test_the_route_and_cookie_name_match_the_contract(self):
        from auth import desktop_contracts as dc
        from auth.desktop_web_session import WEB_SESSION_COOKIE
        from channel.web.route_registry import ROUTES

        self.assertEqual(dc.WEB_SESSION["path"], "/auth/desktop/web-session")
        self.assertEqual(dc.WEB_SESSION["cookie_name"], WEB_SESSION_COOKIE)
        self.assertIn(dc.WEB_SESSION["path"],
                      {entry.pattern for entry in ROUTES})

    def test_the_cookie_attributes_match_the_contract(self):
        from auth import desktop_contracts as dc
        from auth.desktop_web_session import COOKIE_ATTRIBUTES

        attributes = dc.WEB_SESSION["cookie_attributes"]
        self.assertTrue(attributes["http_only"])
        self.assertIn("HttpOnly", COOKIE_ATTRIBUTES)
        self.assertTrue(attributes["secure"])
        self.assertIn("Secure", COOKIE_ATTRIBUTES)
        self.assertEqual(attributes["same_site"].lower(), "lax")
        self.assertIn("SameSite=Lax", COOKIE_ATTRIBUTES)
        self.assertEqual(attributes["path"], "/")
        self.assertIn("Path=/", COOKIE_ATTRIBUTES)
        # Host-only is the *absence* of Domain; a shared cookie is the failure.
        self.assertTrue(attributes["host_only"])
        self.assertNotIn("Domain", COOKIE_ATTRIBUTES)

    def test_ids_and_secret_never_land_in_the_body(self):
        from auth import desktop_contracts as dc
        from auth.desktop_web_session import _ID_PATTERN

        self.assertFalse(dc.WEB_SESSION["secret_in_body_allowed"])
        for minimum in ("bootstrap_id_min_chars", "instance_id_min_chars"):
            self.assertFalse(_ID_PATTERN.fullmatch("x" * (dc.WEB_SESSION[minimum] - 1)))
            self.assertTrue(_ID_PATTERN.fullmatch("x" * dc.WEB_SESSION[minimum]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
