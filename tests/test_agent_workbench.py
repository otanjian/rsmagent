"""Workbench (use-Agents) projection behaviour.

The workbench page reads a minimal whitelisted projection from
``/api/agents?view=workbench``. These tests cover:

- the tenant admin projection is returned when `view` is omitted
- the projection only carries card fields, never workspace/channels/files
- only saved + enabled Agents appear, and the default Agent is flagged

No real registry is used: ``get_agent_registry`` is patched with a small stub
whose ``list()`` returns a couple of ``AgentProfile``-like objects. Auth is
satisfied by a faked ``_db_scope`` yielding a platform-admin RequestContext.
"""

import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Prefer the real web.py package when present so later tests are not polluted
# by an incomplete stub (ThreadedDict ctx / application / etc.).
try:
    import web  # noqa: F401
except ImportError:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.storage = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.ctx = types.SimpleNamespace(env={}, headers=[])
    sys.modules["web"] = web_stub


class _Profile:
    def __init__(self, id, name, enabled=True, description=None, avatar=None,
                 position=None, category=None, tags=None, agent_type="normal",
                 coding_project_dir=None):
        self.id = id
        self.name = name
        self.workspace = f"/tmp/{id}"
        self.enabled = enabled
        self.description = description
        self.avatar = avatar
        self.position = position
        self.category = category
        self.tags = tags or ()
        self.agent_type = agent_type
        self.coding_project_dir = coding_project_dir

    @property
    def is_coding(self):
        return self.agent_type == "coding"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "workspace": self.workspace,
            "enabled": self.enabled,
            "description": self.description,
            "avatar": self.avatar,
            "position": self.position or "",
            "category": self.category or "",
            "tags": list(self.tags or []),
        }


class _Registry:
    default_agent_id = "primary"

    def __init__(self, profiles):
        self._profiles = profiles

    def list(self, include_disabled=False):
        if include_disabled:
            return list(self._profiles)
        return [p for p in self._profiles if p.enabled]


def _admin_ctx():
    from auth.runtime import RequestContext
    return RequestContext(
        user_id="u_admin", username="admin", display_name="Admin",
        is_platform_admin=True, must_change_password=False,
        tenant_id="tnt_test", membership={"id": "m1"},
        permissions={"agent.read", "chat.use", "agent.use"},
        is_tenant_admin=True,
    )


def _fake_db_scope():
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield _admin_ctx()

    return scope()


def _call_get(handler_cls, view=""):
    import channel.web.web_channel as web_channel

    def _scoped_ids(_ctx):
        # The stub registry is not bound to any real tenant, so stand in for the
        # tenant scope: these tests cover the projection shape, not scoping
        # (which tests/test_tenant_default_agent.py pins separately).
        from agent.registry import get_agent_registry
        return [p.id for p in get_agent_registry().list(include_disabled=False)]

    def _binding(_ctx, agent_id):
        # Same stand-in for the object scope: the stub Agents are tenant-shared,
        # so the projection shape is what these tests pin. Ownership and
        # administrator exceptions are pinned by
        # tests/test_object_scope.py and test_private_agent_owner_reachability.py.
        return {"agent_id": agent_id, "tenant_id": "tnt_test"}

    with patch.object(web_channel, "_db_scope", _fake_db_scope), \
         patch.object(web_channel, "_require_read_permission"), \
         patch.object(web_channel, "_tenant_default_agent_id",
                      return_value="primary"), \
         patch.object(web_channel, "_tenant_ids_for_context", _scoped_ids), \
         patch.object(web_channel, "_agent_binding_for", _binding), \
         patch.object(web_channel, "_workbench_chat_readiness",
                      return_value=(True, None)), \
         patch.object(web_channel.web, "header"), \
         patch.object(web_channel.web, "input", return_value=web_channel.web.storage(view=view)):
        return json.loads(handler_cls().GET())


class TestWorkbenchProjection(unittest.TestCase):

    def test_view_workbench_returns_whitelisted_fields_only(self):
        from channel.web.web_channel import AgentsHandler
        profiles = [
            _Profile("primary", "Primary", enabled=True, description="main", avatar=None),
            _Profile("research", "Research", enabled=True, description="researcher"),
            _Profile("archived", "Archived", enabled=False),
        ]
        with patch("agent.registry.get_agent_registry",
                   return_value=_Registry(profiles)):
            data = _call_get(AgentsHandler, view="workbench")

        self.assertEqual(data["status"], "success")
        self.assertEqual(len(data["agents"]), 2)  # archived excluded
        fields = {"id", "name", "description", "avatar", "agent_type", "is_default",
                  "can_chat", "unavailable_reason"}
        for agent in data["agents"]:
            self.assertEqual(set(agent.keys()), fields,
                             "workbench projection must be a strict whitelist")
        # A coding Agent is listed (the console must be able to show it) but
        # never chatted with as if it were a normal runtime.
        self.assertTrue(all(a["agent_type"] == "normal" for a in data["agents"]))
        # No management data leaks into the projection.
        self.assertNotIn("workspace", data)
        self.assertNotIn("channel_instances", data)
        self.assertNotIn("revision", data)
        # Default Agent is flagged.
        by_id = {a["id"]: a for a in data["agents"]}
        self.assertTrue(by_id["primary"]["is_default"])
        self.assertFalse(by_id["research"]["is_default"])
        # Platform admin is execution-authorized for enabled Agents.
        self.assertTrue(all(a["can_chat"] for a in data["agents"]))

    def test_view_workbench_filters_disabled(self):
        from channel.web.web_channel import AgentsHandler
        profiles = [
            _Profile("primary", "Primary", enabled=True),
            _Profile("research", "Research", enabled=False),
        ]
        with patch("agent.registry.get_agent_registry",
                   return_value=_Registry(profiles)):
            data = _call_get(AgentsHandler, view="workbench")
        self.assertEqual([a["id"] for a in data["agents"]], ["primary"])

    def test_view_workbench_empty_response_carries_a_reason_code(self):
        """An empty gallery is not a dead end: it says which empty it is.

        ``empty_reason`` is additive and lives only on the workbench view — the
        management snapshot keeps its existing contract (pinned by
        ``test_default_view_returns_tenant_admin_projection``).
        """
        from channel.web.web_channel import AgentsHandler
        with patch("agent.registry.get_agent_registry", return_value=_Registry([])):
            data = _call_get(AgentsHandler, view="workbench")
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["empty_reason"], "no_agents")

    def test_default_view_returns_tenant_admin_projection(self):
        from channel.web.web_channel import AgentsHandler
        projected = {
            "default_agent_id": "primary",
            "agents": [{"id": "primary", "name": "Primary"}],
        }
        with patch("channel.web.web_channel._tenant_agents_admin_projection",
                   return_value=projected) as proj:
            data = _call_get(AgentsHandler, view="")
        self.assertEqual(data, {"status": "success", **projected})
        proj.assert_called_once()

    def test_default_agent_is_first_even_when_registry_order_differs(self):
        from channel.web.web_channel import AgentsHandler
        registry = _Registry([_Profile("aaa", "Other"), _Profile("primary", "Default")])
        with patch("agent.registry.get_agent_registry", return_value=registry):
            data = _call_get(AgentsHandler, view="workbench")
        self.assertEqual([a["id"] for a in data["agents"]], ["primary", "aaa"])

    def test_readiness_fails_closed_without_a_context(self):
        """The retired legacy consumer must not be advertised as runnable.

        With ``ctx is None`` there is no caller to authorize, and the legacy
        path that used to be open went away with legacy identity mode. The card
        reports ``unauthorized`` rather than promising a chat the send path would
        refuse.
        """
        from channel.web.web_channel import _workbench_chat_readiness
        self.assertEqual(_workbench_chat_readiness(None, "any-agent"),
                         (False, "unauthorized"))

    def test_readiness_database_requires_permission(self):
        """Database mode: read-only caller gets a permission reason, never the
        old ``runtime_not_enabled`` version closure."""
        from auth.runtime import RequestContext
        from channel.web.web_channel import _workbench_chat_readiness

        def _ctx(**over):
            base = dict(user_id="u1", username="u1", display_name="U1",
                        is_platform_admin=False, must_change_password=False,
                        tenant_id="t1", membership=None,
                        permissions={"agent.read"}, is_tenant_admin=False)
            base.update(over)
            return RequestContext(**base)

        class _DenySvc:
            def check_resource_action(self, *a, **kw):
                return False

            def get_agent_binding(self, agent_id):
                # The readiness gate checks the tenant binding before any grant
                # (a platform admin would otherwise pass ``check_resource_action``
                # for another tenant's Agent). This Agent is the caller's own, so
                # the deny path below is what the test exercises.
                return {"agent_id": agent_id, "tenant_id": "t1"}

        with patch("auth.service.get_identity_service",
                   return_value=_DenySvc()):
            can_chat, reason = _workbench_chat_readiness(
                _ctx(), "any-agent")
        self.assertFalse(can_chat)
        self.assertEqual(reason, "permission_denied")

    def test_readiness_database_chat_use_gate(self):
        """Member with agent.use but without chat.use is still not runnable."""
        from auth.runtime import RequestContext
        from channel.web.web_channel import _workbench_chat_readiness

        ctx = RequestContext(
            user_id="u1", username="u1", display_name="U1",
            is_platform_admin=False, must_change_password=False,
            tenant_id="t1", membership=None,
            permissions={"agent.read", "agent.use"}, is_tenant_admin=False)

        class _AllowAgentSvc:
            def check_resource_action(self, *a, **kw):
                return True

            def get_agent_binding(self, agent_id):
                return {"agent_id": agent_id, "tenant_id": "t1"}

            def resolve_default_agent(self, tenant_id, *a, **kw):
                # ``_resolve_default_agent`` reads the anchor *and* its source,
                # so the stub has to answer the richer shape too (task 4.6).
                return {"agent_id": self.resolved_default_agent_id(tenant_id, *a, **kw),
                        "source": None}

            def resolved_default_agent_id(self, tenant_id, *a, **kw):
                # This test pins the functional chat.use gate. There is no tenant
                # default to relax onto, so the Agent stays grant-gated here.
                return None

        with patch("auth.service.get_identity_service",
                   return_value=_AllowAgentSvc()):
            can_chat, reason = _workbench_chat_readiness(ctx, "any-agent")
        self.assertFalse(can_chat)
        self.assertEqual(reason, "permission_denied")

    def test_readiness_database_authorized_runnable(self):
        """Platform admin (and a member with both gates) is runnable."""
        from auth.runtime import RequestContext
        from channel.web.web_channel import _workbench_chat_readiness

        admin = RequestContext(
            user_id="root", username="root", display_name="Root",
            is_platform_admin=True, must_change_password=False,
            tenant_id="t1", membership=None,
            permissions={"agent.read"}, is_tenant_admin=False)
        member = RequestContext(
            user_id="u1", username="u1", display_name="U1",
            is_platform_admin=False, must_change_password=False,
            tenant_id="t1", membership=None,
            permissions={"agent.read", "chat.use", "agent.use"},
            is_tenant_admin=False)

        class _AllowSvc:
            def check_resource_action(self, *a, **kw):
                return True

            def get_agent_binding(self, agent_id):
                return {"agent_id": agent_id, "tenant_id": "t1"}

            def resolve_default_agent(self, tenant_id, *a, **kw):
                # ``_resolve_default_agent`` reads the anchor *and* its source,
                # so the stub has to answer the richer shape too (task 4.6).
                return {"agent_id": self.resolved_default_agent_id(tenant_id, *a, **kw),
                        "source": None}

            def resolved_default_agent_id(self, tenant_id, *a, **kw):
                # This test pins the explicit-grant path; keep the shared-default
                # relaxation out of the way so the grant check is what passes.
                return None

        with patch("auth.service.get_identity_service", return_value=_AllowSvc()):
            self.assertEqual(_workbench_chat_readiness(admin, "any-agent"),
                             (True, None))
            self.assertEqual(_workbench_chat_readiness(member, "any-agent"),
                             (True, None))


class TestWorkbenchFrontEnd(unittest.TestCase):
    """Source-level checks that the console wires the new view + flow."""

    @staticmethod
    def _read(relative):
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def _i18n_text(self):
        """The console's merged locale layer: console.js + i18n namespaces."""
        root = Path(__file__).resolve().parents[1] / "channel/web/static/js"
        parts = [self._read("channel/web/static/js/console.js")]
        parts += [p.read_text(encoding="utf-8")
                  for p in sorted((root / "i18n").glob("*.js"))]
        return "\n".join(parts)

    def test_console_reads_workbench_projection(self):
        js = self._read("channel/web/static/js/console.js")
        assert "fetch('/api/agents?view=workbench'," in js
        assert "function loadAgentWorkbench" in js

    def test_chat_has_workbench_view(self):
        html = self._read("channel/web/chat.html")
        assert 'data-view="agent-workbench"' in html
        assert 'id="view-agent-workbench"' in html
        assert 'id="agent-workbench-grid"' in html

    def test_view_meta_moves_agents_to_manage(self):
        js = self._read("channel/web/static/js/console.js")
        assert "agents:   { group: 'nav_group_agent_dev', page: 'menu_agent_config', console: 'admin.agents' }" in js
        assert "'agent-workbench': { group: 'nav_workbench', page: 'menu_agents', console: 'workbench.agents' }" in js

    def test_config_page_title_and_menu_label(self):
        html = self._read("channel/web/chat.html")
        assert 'menu_agent_config' in html
        assert 'data-i18n="menu_agent_config"' in html

    def test_start_flow_guards_unsaved_before_switch(self):
        js = self._read("channel/web/static/js/console.js")
        assert "function startChatWithAgent" in js
        assert "_agentStartInFlight" in js
        assert "wsGuardUnsaved(() => startChatWithAgent" in js
        assert "resetWorkspaceToAgentRoot" in js
        # No silent default fallback: reject an unknown/disabled target.
        assert "refreshWorkbenchAfterUnavailable" in js

    def test_workbench_i18n_keys_present(self):
        # Task 8.5 split the dictionaries into per-domain namespace files
        # (console.js merges window.__cowI18N__ at load time), so the keys are
        # asserted against the merged locale layer.
        js = self._i18n_text()
        for key in ("agent_workbench_title", "agent_workbench_refresh",
                    "agent_workbench_empty", "agent_workbench_empty_unreachable",
                    "agent_workbench_failed",
                    "agent_target_unavailable", "agent_permission_denied",
                    "start_chat"):
            assert key in js

    def test_workbench_empty_state_is_not_the_failure_state(self):
        js = self._read("channel/web/static/js/console.js")
        # The unreachable wording is chosen by the server's reason code, and the
        # empty branch must keep using the success status line (``setWbStatus``)
        # rather than the failure one (``setWbError``).
        assert "agent_workbench_empty_unreachable" in js
        for code in ("no_agents", "no_reachable_agents"):
            assert code in js
        assert "function _wbEmptyKey" in js

    def test_workbench_permission_denied_label_and_notice(self):
        js = self._read("channel/web/static/js/console.js")
        # Card label + start-failure notice distinguish the permission case from
        # the old version-closure message (task 3.3).
        assert "if (reason === 'permission_denied') return t('agent_permission_denied');" in js
        assert "reason === 'permission_denied'\n        ? 'agent_permission_denied'" in js

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior tests")
    def test_frontend_behavior(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(Path(__file__).with_name("test_agent_workbench_frontend.cjs"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TenantAgentCandidateRangeTestCase(unittest.TestCase):
    """Separating the management set from the chat/use set (task 4.2).

    The management list is where a stopped Agent is found and re-enabled, so it
    keeps disabled Agents; the chat read offers only what can actually be
    chatted with, so it drops them. Both read the same denominator.
    """

    def _candidates(self, include_disabled):
        import channel.web.web_channel as web_channel

        class _RealDefaultRegistry(_Registry):
            # ``AgentRegistry.list`` defaults to *including* disabled Agents and
            # lets the caller narrow the range, so the stub mirrors that instead
            # of the shared ``_Registry`` (whose enabled-only default only fits
            # the projection-shape tests). Otherwise this test would silently
            # pin the stub, not the shipped read.
            def list(self, include_disabled=True):
                return super().list(include_disabled=include_disabled)

        registry = _RealDefaultRegistry([
            _Profile("primary", "Primary"),
            _Profile("archived", "Archived", enabled=False),
        ])
        with patch("agent.registry.get_agent_registry", return_value=registry), \
             patch.object(web_channel, "_tenant_ids_for_context", return_value=None), \
             patch.object(web_channel, "_tenant_default_agent_id",
                          return_value="primary"):
            return [profile.id for profile, _ in web_channel._tenant_agent_candidates(
                _admin_ctx(), include_disabled=include_disabled)]

    def test_the_management_read_keeps_a_disabled_agent(self):
        self.assertEqual(self._candidates(True), ["primary", "archived"])

    def test_the_chat_read_drops_the_disabled_agent(self):
        self.assertEqual(self._candidates(False), ["primary"])


if __name__ == "__main__":
    unittest.main()
