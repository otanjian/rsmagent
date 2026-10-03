# encoding:utf-8
"""审计日志 / Token 消耗 on the real wire (change add-audit-and-token-console).

``tests/test_admin_audit_console_http.py`` proves the handlers' own decisions
with a stubbed ``web`` module; that says nothing about *who reaches them*. These
are the two answers only the assembled app can give: which session the route
policy admits, and whether the read scope the handler derives actually isolates
tenants once the real identity database is behind it.

The failure each assertion exists to catch, in order:

* a member — or nobody — reaching an operator trail at all;
* a **tenant administrator escaping their tenant through ``?tenant=``**, which is
  the one request shape that would turn a filter into a cross-tenant leak;
* a platform administrator's read being silently narrowed to whichever tenant
  they happened to have selected;
* ``/auth/context`` reporting these pages ``consumer_closed`` (a deployment
  claim) when the real reason is the caller's qualification.
"""

import json
import os
import tempfile
import unittest

from tests._helpers import WebAppHarness

ACME_PAGE_KEYS = ("admin.audit", "admin.token_usage")

_EVENTS = "/api/admin/audit/events"
_ACTIONS = "/api/admin/audit/actions"
_USAGE = "/api/admin/token-usage"

_FIXTURE = None


def _status(response):
    """The HTTP status as an int.

    ``web.py`` reports ``response.status`` as a reason phrase ("403 Forbidden"),
    not a WSGI status code.
    """
    raw = str(getattr(response, "status", "") or "")
    return int(raw.split(" ", 1)[0]) if raw[:3].isdigit() else 0


def _json(response):
    try:
        return json.loads(response.data.decode("utf-8"))
    except Exception:
        return {}


def _actions_of(body):
    return [row["action"] for row in body.get("events") or []]


def _build_fixture():
    tmp = tempfile.TemporaryDirectory(prefix="audit-token-console-")
    app = WebAppHarness(os.path.join(tmp.name, "instance"))
    app.add_agent("shared-agent")

    # A second tenant, so "reads only their own tenant" has something to leak.
    other = app.stack.other_tenant(code="other")

    admin_id = app.member("acmeadmin", ["tenant_admin"])
    member_id = app.member("alice", ["member"])
    tokens = {name: app.login(name) for name in ("root", "acmeadmin", "alice")}
    return {"app": app, "tmp": tmp, "other": other, "admin_id": admin_id,
            "member_id": member_id, "tokens": tokens}


def setUpModule():
    global _FIXTURE
    _FIXTURE = _build_fixture()
    _seed(_FIXTURE)


def tearDownModule():
    global _FIXTURE
    if _FIXTURE is not None:
        _FIXTURE["app"].close()
        _FIXTURE["tmp"].cleanup()
        _FIXTURE = None


def _seed(fixture):
    """One audit event and one usage row per tenant, written through real seams.

    The action names are chosen to be unambiguous about *which* tenant's row a
    result came from: ``tenant.copy_agents`` is only ever written for the second
    tenant and ``user.set_status`` only for the platform scope, so an assertion
    that the first tenant's read lacks them is a real isolation claim rather than
    a coincidence of the fixture. The bootstrap itself writes ordinary tenant
    events (``tenant.bootstrap``, ``member.create``), which is why no assertion
    below claims to know the tenant's *whole* action list.
    """
    app = fixture["app"]
    acme = app.tenant_id
    other = fixture["other"]["tenant_id"]
    app.service.record_audit(
        actor_user_id=fixture["member_id"], actor_username="alice",
        tenant_id=acme, target_tenant_id=acme, action="agent.create",
        target="agent:shared-agent", redacted_changes={}, result="success")
    # A denial with only a user ID, as written by many identity mutations.
    app.service.record_audit(
        actor_user_id=fixture["member_id"],
        tenant_id=acme, target_tenant_id=acme, action="agent.delete",
        target="agent:not-mine", redacted_changes={}, result="denied")
    app.service.record_audit(
        actor_username="foreign", tenant_id=other, target_tenant_id=other,
        action="tenant.copy_agents", target="tenant:" + other,
        redacted_changes={}, result="success")
    # A platform-scoped event (no tenant), which only the cross-tenant read sees.
    app.service.record_audit(actor_username="root", tenant_id=None,
                             target_tenant_id=None, action="user.set_status",
                             target="user:n/a", redacted_changes={})

    from agent.token_usage import get_token_usage_manager, record_token_usage

    manager = get_token_usage_manager()
    # The singleton resolves ``identity.db`` from config once; this module builds
    # its own database, so point it at this harness's file before the first read.
    manager._db_path = app.db_path
    manager._store = None
    record_token_usage(provider="deepseek", model="deepseek-chat",
                       prompt_tokens=100, completion_tokens=20,
                       actor_user_id=fixture["member_id"], tenant_id=acme)
    record_token_usage(provider="openai", model="gpt-4o",
                       prompt_tokens=50, completion_tokens=10,
                       actor_user_id=fixture["other"]["user_id"],
                       tenant_id=other)


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _FIXTURE["app"]
        cls.tokens = _FIXTURE["tokens"]
        cls.other = _FIXTURE["other"]

    def get(self, path, name=None, *, tenant=None):
        headers = {"X-Tenant-ID": tenant or self.app.tenant_id}
        return self.app.get(path, token=self.tokens[name] if name else None,
                            headers=headers)

    def context(self, name, *, tenant=None):
        headers = {"X-Tenant-ID": tenant or self.app.tenant_id}
        response = self.app.get("/auth/context", token=self.tokens[name],
                                headers=headers)
        self.assertEqual(_status(response), 200, response.status)
        return _json(response)


class AuditScopeTests(_Fixture):
    """Who may read which tenant's trail, decided on the wire."""

    def test_a_platform_administrator_reads_every_tenant(self):
        body = _json(self.get(_EVENTS, "root"))
        self.assertEqual(body["status"], "success", body)
        self.assertEqual(body["scope"], "all", body)
        tenants = {row["tenant_id"] for row in body["events"]}
        self.assertEqual(tenants, {self.app.tenant_id,
                                   self.other["tenant_id"], ""}, body)

    def test_a_platform_administrator_may_narrow_to_one_tenant(self):
        body = _json(self.get(_EVENTS + "?tenant=" + self.other["tenant_id"],
                              "root"))
        self.assertEqual(body["scope"], "tenant", body)
        self.assertTrue(body["events"], body)
        self.assertEqual({row["tenant_id"] for row in body["events"]},
                         {self.other["tenant_id"]}, body)

    def test_a_tenant_administrator_reads_only_their_own_tenant(self):
        body = _json(self.get(_EVENTS, "acmeadmin"))
        self.assertEqual(body["scope"], "tenant", body)
        self.assertTrue(body["events"], body)
        self.assertEqual({row["tenant_id"] for row in body["events"]},
                         {self.app.tenant_id}, body)

    def test_a_tenant_administrator_cannot_name_another_tenant(self):
        """The one request shape that would turn a filter into a leak."""
        body = _json(self.get(_EVENTS + "?tenant=" + self.other["tenant_id"],
                              "acmeadmin"))
        self.assertEqual(body["scope"], "tenant", body)
        self.assertEqual({row["tenant_id"] for row in body["events"]},
                         {self.app.tenant_id}, body)

    def test_an_ordinary_member_is_refused(self):
        self.assertEqual(_status(self.get(_EVENTS, "alice")), 403)

    def test_no_session_is_refused(self):
        self.assertIn(_status(self.get(_EVENTS)), (401, 403))

    def test_the_failure_filter_keeps_the_denials(self):
        body = _json(self.get(_EVENTS + "?status=failure&actor=ali", "acmeadmin"))
        self.assertEqual(_actions_of(body), ["agent.delete"], body)
        self.assertEqual(body["events"][0]["result"], "denied", body)
        self.assertEqual(body["events"][0]["status"], "failure", body)

    def test_the_success_filter_drops_the_denials(self):
        body = _json(self.get(_EVENTS + "?status=success&actor=ali", "acmeadmin"))
        self.assertEqual(_actions_of(body), ["agent.create"], body)

    def test_id_only_events_show_the_users_name(self):
        body = _json(self.get(_EVENTS + "?action=agent.delete&actor=Alice", "acmeadmin"))
        self.assertEqual(body["total"], 1, body)
        row = body["events"][0]
        self.assertEqual(row["actor"], "Alice")
        self.assertEqual(row["actor_display_name"], "Alice")
        self.assertEqual(row["actor_user_id"], _FIXTURE["member_id"])
        self.assertEqual(row["actor_username"], "")

    def test_the_actor_filter_matches_the_recorded_name(self):
        """Names match both recorded accounts and users resolved by ID."""
        body = _json(self.get(_EVENTS + "?actor=ali", "acmeadmin"))
        self.assertEqual(sorted(_actions_of(body)),
                         ["agent.create", "agent.delete"], body)
        body = _json(self.get(_EVENTS + "?actor=nobody", "acmeadmin"))
        self.assertEqual(body["events"], [], body)

    def test_the_action_list_is_scoped_like_the_events(self):
        body = _json(self.get(_ACTIONS, "acmeadmin"))
        self.assertIn("agent.create", body["actions"], body)
        self.assertNotIn("tenant.copy_agents", body["actions"], body)
        self.assertNotIn("user.set_status", body["actions"], body)
        body = _json(self.get(_ACTIONS, "root"))
        self.assertIn("tenant.copy_agents", body["actions"], body)
        self.assertIn("user.set_status", body["actions"], body)


class TokenUsageScopeTests(_Fixture):
    """The same scope decision, over the metering tables."""

    def _totals(self, name, path):
        body = _json(self.get(path, name))
        self.assertEqual(body["status"], "success", body)
        return body

    def test_a_platform_administrator_sees_every_tenant(self):
        body = self._totals("root", _USAGE + "?type=summary")
        self.assertEqual(body["summary"]["total_prompt_tokens"], 150, body)

    def test_a_tenant_administrator_sees_only_their_own_tenant(self):
        body = self._totals("acmeadmin", _USAGE + "?type=summary")
        self.assertEqual(body["summary"]["total_prompt_tokens"], 100, body)

    def test_a_tenant_administrator_cannot_name_another_tenant(self):
        body = self._totals(
            "acmeadmin",
            _USAGE + "?type=summary&tenant=" + self.other["tenant_id"])
        self.assertEqual(body["summary"]["total_prompt_tokens"], 100, body)
        self.assertEqual(body["scope"], "tenant", body)

    def test_a_platform_administrator_may_narrow_to_one_tenant(self):
        body = self._totals(
            "root", _USAGE + "?type=summary&tenant=" + self.other["tenant_id"])
        self.assertEqual(body["summary"]["total_prompt_tokens"], 50, body)

    def test_each_tab_answers_with_its_own_shape(self):
        for panel, key in (("details", "details"), ("by-user", "users"),
                           ("call_logs", "logs")):
            body = self._totals("acmeadmin", "%s?type=%s" % (_USAGE, panel))
            self.assertIn(key, body, (panel, body))

    def test_the_actor_dropdown_offers_only_the_visible_tenant(self):
        body = self._totals("acmeadmin", _USAGE + "?type=actors")
        self.assertEqual([a["name"] for a in body["actors"]], ["alice"], body)
        body = self._totals("root", _USAGE + "?type=actors")
        self.assertEqual({a["name"] for a in body["actors"]},
                         {"alice", "foreign"}, body)

    def test_an_ordinary_member_is_refused(self):
        self.assertEqual(_status(self.get(_USAGE + "?type=summary", "alice")), 403)

    def test_an_unknown_panel_is_a_400(self):
        self.assertEqual(_status(self.get(_USAGE + "?type=nope", "root")), 400)


class PageProjectionTests(_Fixture):
    """``/auth/context`` must not call these pages closed when they are servable."""

    def test_the_platform_administrator_is_told_they_are_open(self):
        pages = self.context("root")["console_pages"]
        for key in ACME_PAGE_KEYS:
            entry = pages[key]
            self.assertTrue(entry["available"], (key, entry))
            self.assertTrue(entry["read_allowed"], (key, entry))
            self.assertEqual(entry["reason"], "", (key, entry))

    def test_the_tenant_administrator_is_offered_both_pages(self):
        """The qualification that makes the read safe is the one that opens the entry.

        平台管理's platform boundary is per item: 租户管理 / 平台用户管理 / 品牌设置 /
        运行日志 stay platform-only, while these two are offered to the current
        tenant's administrator and narrowed by the handler to their own tenant
        (``test_a_tenant_administrator_cannot_name_another_tenant``). Answering
        ``no_permission`` here would hide a page the handler serves, and
        ``consumer_closed`` / ``deferred`` would be a claim about the deployment.
        """
        pages = self.context("acmeadmin")["console_pages"]
        for key in ACME_PAGE_KEYS:
            entry = pages[key]
            self.assertTrue(entry["available"], (key, entry))
            self.assertTrue(entry["read_allowed"], (key, entry))
            self.assertEqual(entry["reason"], "", (key, entry))

    def test_the_platform_only_rows_beside_them_are_not_offered(self):
        """The boundary moved onto the individual entries — it did not disappear.

        租户管理 spans every tenant. A tenant administrator must not be answered
        as if it were readable just because it now shares a group with two pages
        they are offered.
        """
        pages = self.context("acmeadmin")["console_pages"]
        for key in ("admin.tenants", "admin.settings", "admin.branding",
                    "admin.logs"):
            entry = pages[key]
            self.assertFalse(entry["available"], (key, entry))
            self.assertFalse(entry["read_allowed"], (key, entry))
            self.assertEqual(entry["scope"], "platform", (key, entry))

    def test_a_member_is_never_offered_either_page(self):
        pages = self.context("alice")["console_pages"]
        for key in ACME_PAGE_KEYS:
            entry = pages[key]
            self.assertFalse(entry["available"], (key, entry))
            self.assertFalse(entry["read_allowed"], (key, entry))
            self.assertTrue(entry["reason"], (key, entry))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
