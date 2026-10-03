# encoding:utf-8
"""The HTTP policy gate must decide *before* the handler runs (change group 3).

Before this change ``enforce_http_policy`` only rejected unregistered methods
and short-circuited ``closed`` consumers. For ``tenant``/``platform`` routes it
called ``handler()`` and relied on each handler to resolve the session and
tenant. Handlers that only did ``_require_auth()`` therefore served
tenant-scoped data without any tenant selection at all (``LogsHandler`` is the
measured example), and a missing tenant surfaced as 401 from the handler rather
than the documented 400.

The gate now resolves the request context once (``_require_context``), enforces
the identity domain and the route's declared permission, caches the result for
the handler, and only then calls it. Object-level ownership checks stay in the
handler: the gate is the framework boundary, not a replacement for resource
authorization.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import web

import config
from auth import http_policy
from auth.runtime import IdentityContextError, RequestContext, cached_gate_context
from auth.service import IdentityService
from channel.web import web_channel

ADMIN_PASSWORD = "Str0ngAdminPass"

#: Denial telemetry is persisted to ``identity_db_path``; keep it off the real
#: data dir so unit tests cannot write audit rows into a developer's database.
_TEMP_DB = os.path.join(tempfile.mkdtemp(prefix="http-gate-"), "identity.db")


def _ctx(*, user_id="u_root", tenant_id="tnt_1", permissions=(), is_platform_admin=False,
         is_tenant_admin=True, must_change_password=False):
    return RequestContext(
        user_id=user_id,
        username="root",
        display_name="Root",
        is_platform_admin=is_platform_admin,
        must_change_password=must_change_password,
        tenant_id=tenant_id,
        membership={"id": "m1"} if tenant_id else None,
        permissions=set(permissions),
        is_tenant_admin=is_tenant_admin,
    )


def _status_and_code(result):
    """``(status, code)`` from the ``web.HTTPError`` that ``_json_error`` raises.

    ``web.HTTPError`` stores the body on ``.data`` (its ``args`` only carries the
    status), so the machine-readable ``code`` is read from there.
    """
    status = int(str(result.args[0]).split()[0])
    code = ""
    payload = getattr(result, "data", "")
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if payload:
        try:
            code = json.loads(payload).get("code", "")
        except Exception:
            code = ""
    return status, code


def _run_gate(path, method, *, db_mode=True, resolve=None, settings=None,
              policy_override=None):
    """Invoke the processor with a spy handler; return ``(result, events)``.

    ``resolve`` is called with the ``require_tenant`` flag the gate used. When it
    is ``None`` the gate must not resolve at all, and the patched resolver raises
    to prove it.

    ``policy_override`` forces the matched route entry to a literal policy, which
    is how the ``closed`` branch is covered now that no registered route declares
    it (the recovered slices are all open). Without it a test of that branch would
    depend on a deferred consumer still existing.
    """
    events = []

    def handler():
        events.append("handler")
        return "HANDLER-RAN"

    def resolve_ctx(require_tenant=False):
        events.append("resolve:%s" % require_tenant)
        if resolve is None:
            raise AssertionError("gate resolved %s unexpectedly" % path)
        return resolve(require_tenant)

    web.ctx.path = path
    web.ctx.method = method
    # ``_json_error`` writes response headers through ``web.header``.
    web.ctx.headers = []
    web.ctx.env = {"REQUEST_METHOD": method}
    merged = {"identity_mode": "database", "identity_db_path": _TEMP_DB}
    if settings is not None:
        merged.update(settings)
    patchers = [
        patch.object(http_policy, "_is_database_mode", return_value=db_mode),
        patch("channel.web.auth_handlers._require_context", side_effect=resolve_ctx),
        patch.object(config, "conf", return_value=merged),
    ]
    if policy_override is not None:
        patchers.append(patch.object(
            http_policy, "_match_policy",
            return_value=({"policy": policy_override}, True)))
    for p in patchers:
        p.start()
    try:
        return http_policy.enforce_http_policy(handler), events
    except web.HTTPError as exc:
        return exc, events
    finally:
        for p in reversed(patchers):
            p.stop()


class GateDecisionTests(unittest.TestCase):
    """Unit-level: the gate's decision and the order of operations."""

    def test_tenant_route_without_selection_is_400_before_handler(self):
        def resolve(require_tenant):
            self.assertTrue(require_tenant, "a tenant route must resolve a tenant")
            raise IdentityContextError("tenant selection required", "missing_tenant", 400)

        result, events = _run_gate("/api/tenant/permissions", "GET", resolve=resolve)
        self.assertEqual(_status_and_code(result), (400, "missing_tenant"))
        self.assertNotIn("handler", events)

    def test_tenant_route_without_membership_is_403_before_handler(self):
        def resolve(require_tenant):
            raise IdentityContextError("forbidden", "forbidden", 403)

        result, events = _run_gate("/api/tenant/permissions", "GET", resolve=resolve)
        self.assertEqual(_status_and_code(result), (403, "forbidden"))
        self.assertNotIn("handler", events)

    def test_platform_route_without_credentials_is_401_before_handler(self):
        def resolve(require_tenant):
            self.assertFalse(require_tenant, "a platform route ignores tenant selection")
            raise IdentityContextError("unauthorized", "unauthorized", 401)

        result, events = _run_gate("/api/platform/users", "GET", resolve=resolve)
        self.assertEqual(_status_and_code(result), (401, "unauthorized"))
        self.assertNotIn("handler", events)

    def test_platform_route_without_admin_is_403_before_handler(self):
        result, events = _run_gate("/api/platform/users", "GET",
                                   resolve=lambda rt: _ctx(tenant_id=None))
        self.assertEqual(_status_and_code(result)[0], 403)
        self.assertNotIn("handler", events)

    def test_platform_route_with_admin_passes(self):
        result, events = _run_gate("/api/platform/users", "GET",
                                   resolve=lambda rt: _ctx(tenant_id=None,
                                                           is_platform_admin=True))
        self.assertEqual(result, "HANDLER-RAN")
        self.assertIn("handler", events)

    def test_declared_permission_is_enforced_by_the_gate(self):
        result, events = _run_gate("/api/tenant", "GET",
                                   resolve=lambda rt: _ctx(permissions=()))
        self.assertEqual(_status_and_code(result), (403, "forbidden"))
        self.assertNotIn("handler", events)

    def test_declared_permission_holder_passes(self):
        result, events = _run_gate("/api/tenant", "GET",
                                   resolve=lambda rt: _ctx(permissions={"tenant.info.read"}))
        self.assertEqual(result, "HANDLER-RAN")
        self.assertIn("handler", events)

    def test_platform_admin_is_unrestricted_for_a_declared_permission(self):
        result, events = _run_gate("/api/tenant", "GET",
                                   resolve=lambda rt: _ctx(is_platform_admin=True,
                                                           permissions=()))
        self.assertEqual(result, "HANDLER-RAN")

    def test_route_without_a_declared_permission_needs_no_permission(self):
        result, events = _run_gate("/api/tenant/permissions", "GET",
                                   resolve=lambda rt: _ctx(permissions=()))
        self.assertEqual(result, "HANDLER-RAN")


class GateContextCacheTests(unittest.TestCase):
    def test_resolved_context_is_cached_for_the_handler(self):
        ctx = _ctx(tenant_id="tnt_1")
        seen = {}

        def handler():
            seen["cached"] = cached_gate_context(True)
            return "OK"

        web.ctx.path = "/api/tenant/permissions"
        web.ctx.method = "GET"
        web.ctx.headers = []
        web.ctx.env = {"REQUEST_METHOD": "GET"}
        with patch.object(http_policy, "_is_database_mode", return_value=True), \
                patch("channel.web.auth_handlers._require_context", return_value=ctx):
            self.assertEqual(http_policy.enforce_http_policy(handler), "OK")

        self.assertIs(seen["cached"], ctx)
        # The cache is request-scoped: it must not leak to the next request on
        # the same pooled thread.
        self.assertIsNone(cached_gate_context(True))

    def test_cache_is_keyed_by_the_identity_domain(self):
        """A personal context must not satisfy a tenant lookup (and vice versa)."""
        ctx = _ctx(tenant_id=None, is_platform_admin=True)
        seen = {}

        def handler():
            seen["tenant"] = cached_gate_context(True)
            seen["personal"] = cached_gate_context(False)
            return "OK"

        web.ctx.path = "/api/platform/users"
        web.ctx.method = "GET"
        web.ctx.headers = []
        web.ctx.env = {"REQUEST_METHOD": "GET"}
        with patch.object(http_policy, "_is_database_mode", return_value=True), \
                patch("channel.web.auth_handlers._require_context", return_value=ctx):
            http_policy.enforce_http_policy(handler)

        self.assertIs(seen["personal"], ctx)
        self.assertIsNone(seen["tenant"])


class GateScopeTests(unittest.TestCase):
    """``public``/``closed`` routes are not resolved; legacy mode is a no-op."""

    def test_public_route_is_not_resolved(self):
        result, events = _run_gate("/api/health", "GET", resolve=None)
        self.assertEqual(result, "HANDLER-RAN")
        self.assertEqual(events, ["handler"])

    def test_closed_route_in_database_never_resolves_or_runs_the_handler(self):
        """A ``closed`` declaration is refused before resolution and the handler.

        No route in the current registry declares ``closed`` any more: the
        recovered consumers were adapted and their slices opened. The guarantee
        still has to be covered, so the entry is declared closed here rather than
        relying on a live deferred route to keep this branch exercised.
        """
        result, events = _run_gate("/api/scheduler", "GET", resolve=None,
                                   policy_override="closed")
        self.assertEqual(_status_and_code(result), (503, "database_unavailable"))
        self.assertNotIn("handler", events)

    def test_legacy_mode_is_a_no_op(self):
        result, events = _run_gate("/api/tenant/permissions", "GET", db_mode=False, resolve=None)
        self.assertEqual(result, "HANDLER-RAN")
        self.assertEqual(events, ["handler"])

    def test_unknown_url_stays_404(self):
        result, events = _run_gate("/does/not/exist", "GET", resolve=None)
        self.assertEqual(result, "HANDLER-RAN")  # web.py notfound owns this
        self.assertEqual(events, ["handler"])

    def test_route_deriving_its_tenant_from_the_resource_skips_selection(self):
        """``GET /stream`` must not demand ``X-Tenant-ID``.

        Native ``EventSource`` sends the session cookie but cannot add a header,
        so the SSE reconnect derives the tenant from the owned request
        (``web_channel._stream_identity_scope``). The gate still authenticates the
        caller -- it just does not require a selection for this route.
        """
        result, events = _run_gate("/stream", "GET",
                                   resolve=lambda rt: _ctx(tenant_id=None))
        self.assertEqual(result, "HANDLER-RAN")
        self.assertEqual(events, ["resolve:False", "handler"])

    def test_resource_derived_tenant_still_requires_a_session(self):
        def resolve(require_tenant):
            raise IdentityContextError("unauthorized", "unauthorized", 401)

        result, events = _run_gate("/stream", "GET", resolve=resolve)
        self.assertEqual(_status_and_code(result), (401, "unauthorized"))
        self.assertNotIn("handler", events)


class GateFailureTests(unittest.TestCase):
    """Unexpected failures never authorize a handler, including old configs."""

    def test_unexpected_failure_denies_by_default(self):
        def resolve(require_tenant):
            raise RuntimeError("identity store unreachable")

        result, events = _run_gate("/api/tenant/permissions", "GET", resolve=resolve)
        self.assertEqual(_status_and_code(result), (503, "identity_unavailable"))
        self.assertNotIn("handler", events)

    def test_old_observation_setting_cannot_bypass_tenant_or_platform_gate(self):
        def resolve(require_tenant):
            raise RuntimeError("SYNTHETIC_PRIVATE_VALUE")
        settings = {"http_policy_gate_fail_closed": False}
        for path in ("/api/tenant/permissions", "/api/platform/users"):
            with self.subTest(path=path), self.assertLogs("http_policy", level="WARNING") as logs:
                result, events = _run_gate(path, "GET", resolve=resolve, settings=settings)
                self.assertEqual(_status_and_code(result), (503, "identity_unavailable"))
                self.assertNotIn("handler", events)
                self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", str(logs.output))

    def test_recovery_rechecks_and_restores_authorized_access(self):
        def unavailable(require_tenant):
            raise RuntimeError("temporary failure")
        failed, events = _run_gate("/api/tenant/permissions", "GET", resolve=unavailable)
        self.assertEqual(_status_and_code(failed)[0], 503)
        self.assertNotIn("handler", events)
        recovered, events = _run_gate("/api/tenant/permissions", "GET", resolve=lambda _: _ctx())
        self.assertEqual(recovered, "HANDLER-RAN")
        self.assertEqual(events, ["resolve:True", "handler"])

    def test_unexpected_failure_denies_when_fail_closed_is_enabled(self):
        def resolve(require_tenant):
            raise RuntimeError("identity store unreachable")

        settings = {"identity_mode": "database", "http_policy_gate_fail_closed": True}
        result, events = _run_gate("/api/tenant/permissions", "GET", resolve=resolve, settings=settings)
        self.assertEqual(_status_and_code(result), (503, "identity_unavailable"))
        self.assertNotIn("handler", events)

    def test_deterministic_failures_are_never_deferred(self):
        """Unexpected-failure handling must not soften a real 400/401/403."""
        def resolve(require_tenant):
            raise IdentityContextError("forbidden", "forbidden", 403)

        settings = {"identity_mode": "database"}  # fail-closed knob intentionally absent
        result, events = _run_gate("/api/tenant/permissions", "GET", resolve=resolve, settings=settings)
        self.assertEqual(_status_and_code(result), (403, "forbidden"))
        self.assertNotIn("handler", events)


class GateIntegrationTests(unittest.TestCase):
    """Observed through the real app factory, which installs the processor."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "identity.db")
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password=ADMIN_PASSWORD,
            shared_root="/s/acme", allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.login = self.svc.login("root", ADMIN_PASSWORD)
        self.token = self.login.token

    def _patch_db(self):
        settings = {"identity_mode": "database", "identity_db_path": self.db}
        for p in (patch.object(config, "conf", return_value=settings),
                  patch.object(web_channel, "conf", return_value=settings)):
            p.start()
            self.addCleanup(p.stop)

    def _request(self, path, method="GET", headers=None):
        app = web_channel.build_web_app()
        merged = {"Host": "test"}
        if headers:
            merged.update(headers)
        return app.request(path, method=method, headers=merged)

    def test_tenant_route_without_selection_is_rejected_before_the_handler(self):
        """``/api/tenant/permissions`` only calls ``_require_auth()``: without a
        tenant selection the gate must refuse, not serve the tenant catalog."""
        self._patch_db()
        resp = self._request("/api/tenant/permissions",
                             headers={"Cookie": f"cow_session={self.token}"})
        self.assertEqual(resp.status, "400 Bad Request")
        self.assertIn(b"missing_tenant", resp.data)

    def test_tenant_route_with_selection_but_no_session_is_401(self):
        self._patch_db()
        resp = self._request("/api/tenant/permissions", headers={"X-Tenant-ID": self.tid})
        self.assertEqual(resp.status, "401 Unauthorized")

    def test_tenant_route_with_a_valid_context_reaches_the_handler(self):
        self._patch_db()
        resp = self._request("/api/tenant", headers={
            "Cookie": f"cow_session={self.token}", "X-Tenant-ID": self.tid})
        self.assertEqual(resp.status, "200 OK")
        payload = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(payload["status"], "success")

    def test_anonymous_platform_route_is_401_not_a_handler_response(self):
        self._patch_db()
        resp = self._request("/api/platform/users")
        self.assertEqual(resp.status, "401 Unauthorized")

    def test_a_browser_navigation_can_still_load_the_console_shell(self):
        """The SPA shell is a document, not tenant data.

        A top-level browser navigation cannot send ``X-Tenant-ID``, so the gate
        must not demand a selection for ``GET /chat`` / ``GET /admin``: doing so
        returned 400 to every browser visit (``/`` redirects to ``/chat``) and
        the login UI never rendered. The shell carries no tenant data — every
        data API behind it keeps its own ``tenant``/``platform`` policy.
        """
        self._patch_db()
        for path in ("/chat", "/admin"):
            with self.subTest(path=path):
                resp = self._request(path, headers={
                    "Accept": "text/html,application/xhtml+xml"})
                self.assertEqual(resp.status, "200 OK")
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))

    def test_the_shell_is_still_served_to_an_authenticated_browser(self):
        self._patch_db()
        resp = self._request("/chat", headers={
            "Accept": "text/html", "Cookie": f"cow_session={self.token}"})
        self.assertEqual(resp.status, "200 OK")

    def test_gate_does_not_replace_object_level_ownership_checks(self):
        """A valid context is not an authorization to any object.

        The gate admits the request (its mandate is context + domain +
        permission); the handler's own ownership check is what refuses. The spy
        below stands in for ``_require_session_scope`` and records that the
        handler really ran and really saw the caller's context.
        """
        self._patch_db()
        calls = []
        real_scope = web_channel._require_session_scope

        def spy(ctx, session_id, agent_id=""):
            calls.append((ctx.user_id, session_id, cached_gate_context(True) is ctx))
            raise web.HTTPError(
                "403 Forbidden",
                {"Content-Type": "application/json; charset=utf-8"},
                json.dumps({"status": "error", "message": "not yours",
                            "code": "not_yours"}))

        with patch.object(web_channel, "_require_session_scope", side_effect=spy):
            resp = self._request("/api/sessions/sess_not_mine", method="DELETE", headers={
                "Cookie": f"cow_session={self.token}", "X-Tenant-ID": self.tid})

        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0][0], calls[0][1]), (self.login.user_id, "sess_not_mine"))
        # The handler ran on the very context the gate resolved, not a re-read.
        self.assertIs(calls[0][2], True)
        self.assertEqual(resp.status, "403 Forbidden")
        self.assertIn(b"not_yours", resp.data)
        self.assertNotIn(b"missing_tenant", resp.data)
        self.assertIsNotNone(real_scope)


if __name__ == "__main__":
    unittest.main()
