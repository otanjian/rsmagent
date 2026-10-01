# encoding:utf-8
"""The local project root: where it lives, who may register it, what it grants.

Change ``align-desktop-project-execution-with-master`` (tasks 3.1 / 3.2 / 3.3).

Three separable properties, one file:

* the **execution target** is an authorization, not a path -- a read-only file
  reference must never be promotable to local execution by the transport that
  registers a root, and an unknown purpose is refused rather than normalised;
* the **registry** that resolves a target to a directory is scoped and
  versioned, so a re-picked directory (a new ``grant_version``) invalidates the
  old entry instead of silently continuing to serve the previous root;
* a **registration** is loopback + per-launch-token + native-only, and the
  absolute path is never persisted nor echoed.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
NONCE = "nonce_" + ("b" * 22)
SESSION_A = "biz-session-a"
SESSION_B = "biz-session-b"
AGENT = "desk-agent"
DESKTOP_TOKEN = "desktop-token-for-tests"


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    """Stand-in for a phase-2 capability that has passed its gates."""

    enabled = True

    def is_open(self, action):
        return True


# ---------------------------------------------------------------------------
# The execution target value
# ---------------------------------------------------------------------------


class ExecutionTargetTests(unittest.TestCase):
    """A target is identifiers plus a purpose. It never holds a path."""

    def test_backend_is_the_unset_value(self):
        from agent.workspace.execution_target import (
            ExecutionTarget, LOCATION_BACKEND, MODE_READONLY_INPUT,
        )
        from agent.workspace.execution_target import BACKEND_TARGET

        self.assertEqual(BACKEND_TARGET.location, LOCATION_BACKEND)
        self.assertFalse(BACKEND_TARGET.is_desktop)
        self.assertFalse(BACKEND_TARGET.allows_project_execution)
        self.assertEqual(BACKEND_TARGET.project_mode, MODE_READONLY_INPUT)
        self.assertEqual(ExecutionTarget(), BACKEND_TARGET)

    def test_desktop_requires_every_identifier(self):
        from agent.workspace.execution_target import (
            ExecutionTargetError, desktop_target,
        )
        for missing in ("device_id", "workspace_id", "binding_id"):
            fields = {"device_id": "dev_1", "workspace_id": "ws_1",
                      "binding_id": "bind_1"}
            fields[missing] = ""
            with self.assertRaises(ExecutionTargetError) as caught:
                desktop_target(**fields)
            self.assertIn(missing, str(caught.exception))

    def test_backend_must_not_carry_desktop_identifiers(self):
        """A half-migrated record could be read as a grant; refuse it."""
        from agent.workspace.execution_target import (
            ExecutionTarget, ExecutionTargetError, LOCATION_BACKEND,
        )
        with self.assertRaises(ExecutionTargetError):
            ExecutionTarget(location=LOCATION_BACKEND, device_id="dev_1")

    def test_only_project_execution_allows_execution(self):
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, MODE_READONLY_INPUT, desktop_target,
        )
        readonly = desktop_target(
            device_id="dev_1", workspace_id="ws_1", binding_id="bind_1",
            project_mode=MODE_READONLY_INPUT)
        runnable = desktop_target(
            device_id="dev_1", workspace_id="ws_1", binding_id="bind_1",
            project_mode=MODE_PROJECT_EXECUTION)
        self.assertTrue(readonly.is_readonly_input)
        self.assertFalse(readonly.allows_project_execution)
        self.assertTrue(runnable.allows_project_execution)
        self.assertFalse(runnable.is_readonly_input)

    def test_unknown_location_or_mode_is_refused(self):
        from agent.workspace.execution_target import (
            ExecutionTarget, ExecutionTargetError, MODE_READONLY_INPUT,
        )
        with self.assertRaises(ExecutionTargetError):
            ExecutionTarget(location="somewhere-else")
        with self.assertRaises(ExecutionTargetError):
            ExecutionTarget(project_mode="read-write")

    def test_roundtrip_through_dict(self):
        from agent.workspace.execution_target import (
            ExecutionTarget, MODE_PROJECT_EXECUTION, desktop_target,
        )
        original = desktop_target(
            device_id="dev_1", workspace_id="ws_1", binding_id="bind_1",
            project_mode=MODE_PROJECT_EXECUTION, grant_version=3,
            selection_generation=7)
        restored = ExecutionTarget.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_stored_backend_target_reads_as_none(self):
        """Legacy/other deployments stored nothing; both mean "no target"."""
        from agent.workspace.execution_target import ExecutionTarget

        self.assertIsNone(ExecutionTarget.from_dict(None))
        self.assertIsNone(ExecutionTarget.from_dict({"location": "backend"}))
        # An empty desktop-ish record is refused, not silently accepted.
        from agent.workspace.execution_target import ExecutionTargetError
        with self.assertRaises(ExecutionTargetError):
            ExecutionTarget.from_dict({"location": "desktop"})

    def test_serialized_form_has_no_path_key(self):
        from agent.workspace.execution_target import desktop_target

        payload = desktop_target(
            device_id="dev_1", workspace_id="ws_1", binding_id="bind_1").to_dict()
        for forbidden in ("path", "absolute_path", "root", "cwd", "dir"):
            self.assertNotIn(forbidden, payload)

    def test_overlong_identifier_is_refused(self):
        from agent.workspace.execution_target import (
            ExecutionTargetError, desktop_target,
        )
        with self.assertRaises(ExecutionTargetError):
            desktop_target(device_id="d" * 129, workspace_id="ws_1",
                           binding_id="bind_1")


# ---------------------------------------------------------------------------
# The in-memory registry (target -> directory)
# ---------------------------------------------------------------------------


class LocalRootRegistryTests(unittest.TestCase):
    """Scope math: exact match or nothing, and a new version invalidates."""

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry

        self.registry = LocalRootRegistry()
        self.base = dict(
            user_id="u1", tenant_id="tnt_1", device_id="dev_1",
            workspace_id="ws_1", binding_id="bind_1", grant_version=1,
            project_mode="readonly-input",
        )

    def register(self, **overrides):
        fields = dict(self.base)
        fields.update(overrides)
        if "absolute_path" not in fields:
            fields["absolute_path"] = "/tmp/project"
        return self.registry.register(**fields)

    def lookup(self, **overrides):
        fields = dict(
            user_id="u1", tenant_id="tnt_1", device_id="dev_1",
            workspace_id="ws_1", binding_id="bind_1", grant_version=1,
        )
        fields.update(overrides)
        return self.registry.lookup(**fields)

    def test_register_then_lookup(self):
        self.register(absolute_path="/tmp/project")
        entry = self.lookup()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.absolute_path, "/tmp/project")

    def test_path_is_normalised(self):
        self.register(absolute_path="/tmp/a/../project/")
        self.assertEqual(self.lookup().absolute_path,
                         os.path.normpath("/tmp/project"))

    def test_relative_path_is_refused(self):
        from agent.desktop_local import LocalRootError

        with self.assertRaises(LocalRootError) as caught:
            self.register(absolute_path="project")
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_empty_or_nul_path_is_refused(self):
        from agent.desktop_local import LocalRootError

        for bad in ("", "   ", "/tmp/a\x00b"):
            with self.assertRaises(LocalRootError):
                self.register(absolute_path=bad)

    def test_unknown_mode_is_refused_at_the_registry_too(self):
        """Defence in depth: the value cannot be constructed anywhere."""
        from agent.desktop_local import LocalRootError

        with self.assertRaises(LocalRootError):
            self.register(project_mode="project-execution-ish")

    def test_non_positive_grant_version_is_refused(self):
        from agent.desktop_local import LocalRootError

        for bad in (0, -1, None, "abc"):
            with self.assertRaises(LocalRootError):
                self.register(grant_version=bad)

    def test_version_bump_invalidates_the_old_root(self):
        """Re-picking a directory must not keep serving the previous one."""
        self.register(absolute_path="/tmp/old", grant_version=1)
        self.register(absolute_path="/tmp/new", grant_version=2)
        self.assertIsNone(self.lookup(grant_version=1))
        self.assertEqual(self.lookup(grant_version=2).absolute_path, "/tmp/new")

    def test_scope_mismatch_never_falls_back_to_a_nearby_entry(self):
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        self.register(project_mode=MODE_PROJECT_EXECUTION)
        # Every identifier is part of the key: a mismatch is a miss, never a
        # fallback to "the closest entry".
        self.assertIsNone(self.lookup(device_id="dev_2"))
        self.assertIsNone(self.lookup(workspace_id="ws_2"))
        self.assertIsNone(self.lookup(binding_id="bind_2"))
        self.assertIsNone(self.lookup(user_id="u2"))
        self.assertIsNone(self.lookup(tenant_id="tnt_2"))
        self.assertIsNotNone(self.lookup())

    def test_require_mode_is_enforced(self):
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, MODE_READONLY_INPUT,
        )
        self.register(project_mode=MODE_READONLY_INPUT)
        self.assertIsNotNone(self.lookup(require_mode=MODE_READONLY_INPUT))
        self.assertIsNone(self.lookup(require_mode=MODE_PROJECT_EXECUTION))

    def test_revoke_is_narrow(self):
        self.register()
        self.register(device_id="dev_2", workspace_id="ws_2",
                      binding_id="bind_2", absolute_path="/tmp/other")
        self.assertEqual(self.registry.revoke(device_id="dev_1"), 1)
        self.assertIsNone(self.lookup())
        self.assertIsNotNone(self.lookup(device_id="dev_2", workspace_id="ws_2",
                                         binding_id="bind_2"))

    def test_revoke_everything_for_a_user(self):
        self.register()
        self.register(device_id="dev_2", workspace_id="ws_2",
                      binding_id="bind_2", absolute_path="/tmp/other")
        self.assertEqual(self.registry.revoke(user_id="u1"), 2)
        self.assertEqual(len(self.registry), 0)

    def test_clear_empties_the_registry(self):
        self.register()
        self.assertEqual(self.registry.clear(), 1)
        self.assertEqual(len(self.registry), 0)


# ---------------------------------------------------------------------------
# Session persistence of the target
# ---------------------------------------------------------------------------


class LocalRootLookupByPathTests(unittest.TestCase):
    """Which live project holds a file (task 9.6).

    History replay has to answer one question about a recorded absolute path:
    *which* authorization produced it. The answer decides both the card's
    identity (device/workspace/binding/relative path) and whether the card may
    be acted on at all, so it must come from the registration that really holds
    the file rather than from whatever project the session is bound to *now*.
    """

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry

        self.registry = LocalRootRegistry()
        self._tmp = tempfile.TemporaryDirectory(prefix="root-lookup-")
        self.addCleanup(self._tmp.cleanup)
        self.base = dict(
            user_id="u1", tenant_id="tnt_1", device_id="dev_1",
            workspace_id="ws_1", binding_id="bind_1", grant_version=1,
            project_mode="project-execution",
        )

    def _dir(self, *parts):
        path = os.path.join(self._tmp.name, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def register(self, absolute_path, **overrides):
        fields = dict(self.base)
        fields.update(overrides)
        fields["absolute_path"] = absolute_path
        return self.registry.register(**fields)

    def found(self, path, user_id="u1", tenant_id="tnt_1"):
        return self.registry.entry_for_path(
            path, user_id=user_id, tenant_id=tenant_id)

    def test_a_registered_root_answers_which_project_holds_a_file(self):
        root = self._dir("project")
        self.register(root)

        entry = self.found(os.path.join(root, "output", "report.txt"))

        self.assertIsNotNone(entry)
        self.assertEqual(entry.absolute_path, os.path.normpath(root))
        self.assertEqual(entry.workspace_id, "ws_1")

    def test_the_innermost_project_wins(self):
        """A nested project's file must not be filed under the outer one.

        Both roots really hold the path; the card needs the *closest*
        authorization, or a file produced in the inner project would be
        attributed to the outer project's workspace and relative path.
        """
        outer = self._dir("outer")
        inner = self._dir("outer", "inner")
        self.register(outer, workspace_id="ws_outer", binding_id="bind_outer")
        self.register(inner, workspace_id="ws_inner", binding_id="bind_inner")

        entry = self.found(os.path.join(inner, "report.txt"))

        self.assertIsNotNone(entry)
        self.assertEqual(entry.workspace_id, "ws_inner")

    def test_a_path_outside_every_root_is_no_project(self):
        root = self._dir("project")
        self.register(root)

        self.assertIsNone(self.found(os.path.join(self._tmp.name, "elsewhere", "x")))

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self):
        """``/tmp/project-ab`` is not inside ``/tmp/project``."""
        root = self._dir("project")
        sibling = self._dir("project-ab")
        self.register(root)

        self.assertIsNone(self.found(os.path.join(sibling, "report.txt")))

    def test_the_root_itself_counts_as_held(self):
        root = self._dir("project")
        self.register(root)

        self.assertIsNotNone(self.found(root))

    def test_another_users_registration_is_not_usable(self):
        """The other user's project on the same machine is not this user's."""
        root = self._dir("project")
        self.register(root, user_id="u2", tenant_id="tnt_1")

        self.assertIsNone(self.found(os.path.join(root, "report.txt"), user_id="u1"))

    def test_another_tenants_registration_is_not_usable(self):
        root = self._dir("project")
        self.register(root, tenant_id="tnt_2")

        self.assertIsNone(self.found(os.path.join(root, "report.txt")))

    def test_a_revoked_root_is_not_resurrected(self):
        """A re-picked directory must not come back as "recently used"."""
        root = self._dir("project")
        self.register(root)
        self.registry.revoke(user_id="u1", tenant_id="tnt_1", device_id="dev_1")

        self.assertIsNone(self.found(os.path.join(root, "report.txt")))

    def test_a_relative_or_empty_path_is_no_project(self):
        """Bad input answers "no project"; it must not raise into a reply."""
        self.register(self._dir("project"))

        for bad in ("", "   ", "output/report.txt", None, "/tmp/x\x00y"):
            with self.subTest(path=bad):
                self.assertIsNone(
                    self.registry.entry_for_path(bad, user_id="u1",
                                                 tenant_id="tnt_1"))

    def test_a_link_out_of_the_project_does_not_hold_its_target(self):
        """realpath on both sides, so a link cannot smuggle a path into a project.

        The judgement has to be real-to-real for the same reason the panel's
        include checks are: comparing the joined path would accept a link placed
        inside the project that points anywhere on the disk, and the card would
        then name a project that does not actually hold the file.
        """
        root = self._dir("project")
        outside = self._dir("outside")
        os.symlink(outside, os.path.join(root, "linked"))
        self.register(root)

        self.assertIsNone(self.found(os.path.join(root, "linked", "report.txt")))
        self.assertIsNone(self.found(os.path.join(outside, "report.txt")))

    def test_a_file_reached_through_a_link_that_stays_inside_is_held(self):
        """The rule is about where the bytes are, not about how they were named."""
        root = self._dir("project")
        real = self._dir("project", "real")
        os.symlink(real, os.path.join(root, "alias"))
        self.register(root)

        self.assertIsNotNone(self.found(os.path.join(root, "alias", "report.txt")))


class ProjectStoreTargetTests(unittest.TestCase):
    """The store keeps identifiers only, and drops them with the session."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="desktop-target-")
        self.addCleanup(self._tmp.cleanup)
        from agent.workspace import project_store
        self.store = project_store
        self._patch = patch.object(
            project_store, "_store_file", return_value=os.path.join(
                self._tmp.name, "projects.json"))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "dev_1", "workspace_id": "ws_1",
                  "binding_id": "bind_1"}
        fields.update(overrides)
        return desktop_target(**fields)

    def test_absent_is_none(self):
        self.assertIsNone(self.store.get_execution_target("s1", "agent-a"))

    def test_set_then_get(self):
        self.store.set_execution_target("s1", self.target(), "agent-a")
        stored = self.store.get_execution_target("s1", "agent-a")
        self.assertIsNotNone(stored)
        self.assertTrue(stored.is_desktop)
        self.assertEqual(stored.workspace_id, "ws_1")

    def test_targets_are_namespaced_by_agent(self):
        self.store.set_execution_target("s1", self.target(), "agent-a")
        self.assertIsNone(self.store.get_execution_target("s1", "agent-b"))
        self.assertIsNone(self.store.get_execution_target("s1"))
        self.assertIsNotNone(self.store.get_execution_target("s1", "agent-a"))

    def test_clear_and_delete_session_both_forget(self):
        self.store.set_execution_target("s1", self.target(), "agent-a")
        self.store.clear_execution_target("s1", "agent-a")
        self.assertIsNone(self.store.get_execution_target("s1", "agent-a"))

        self.store.set_execution_target("s2", self.target(), "agent-a")
        self.store.forget_session("s2", "agent-a")
        self.assertIsNone(self.store.get_execution_target("s2", "agent-a"))

    def test_only_a_desktop_target_can_be_stored(self):
        from agent.workspace.execution_target import BACKEND_TARGET

        with self.assertRaises(ValueError):
            self.store.set_execution_target("s1", BACKEND_TARGET, "agent-a")

    def test_absolute_path_is_never_persisted(self):
        self.store.set_execution_target("s1", self.target(), "agent-a")
        with open(os.path.join(self._tmp.name, "projects.json"),
                  encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("/Users/", raw)
        self.assertNotIn("absolute_path", raw)

    def test_an_unreadable_record_reads_as_no_target(self):
        """A record from a future/unknown shape must not become a live grant."""
        import json

        path = os.path.join(self._tmp.name, "projects.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"sessions": {}, "recents": [], "meta": {}, "order": [],
                       "desktop_targets": {
                           "agent-a::s1": {"location": "desktop"}}}, handle)
        self.assertIsNone(self.store.get_execution_target("s1", "agent-a"))

    def test_legacy_store_without_the_key_still_works(self):
        import json

        path = os.path.join(self._tmp.name, "projects.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"sessions": {}, "recents": [], "meta": {}, "order": []},
                      handle)
        self.assertIsNone(self.store.get_execution_target("s1", "agent-a"))
        # ...and writing is a no-op upgrade rather than a crash.
        self.store.set_execution_target("s1", self.target(), "agent-a")
        self.assertIsNotNone(self.store.get_execution_target("s1", "agent-a"))


# ---------------------------------------------------------------------------
# The transport gate
# ---------------------------------------------------------------------------


class TransportGuardTests(unittest.TestCase):
    """Loopback and the per-launch token, checked for real."""

    def setUp(self):
        import web

        self.web = web
        web.ctx.clear()
        self.addCleanup(lambda: web.ctx.clear())

    def test_off_loopback_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        from integrations.desktop.local_root import _guard_transport

        self.web.ctx.env = {"REMOTE_ADDR": "10.1.2.3"}
        with self.assertRaises(DesktopAccessError) as caught:
            _guard_transport()
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_forwarded_request_is_refused_even_from_loopback(self):
        from integrations.desktop.errors import DesktopAccessError
        from integrations.desktop.local_root import _guard_transport

        self.web.ctx.env = {"REMOTE_ADDR": "127.0.0.1",
                            "HTTP_X_FORWARDED_FOR": "10.1.2.3"}
        with self.assertRaises(DesktopAccessError):
            _guard_transport()

    def test_missing_launch_token_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        from integrations.desktop.local_root import _guard_transport

        self.web.ctx.env = {"REMOTE_ADDR": "127.0.0.1"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COW_DESKTOP_TOKEN", None)
            with self.assertRaises(DesktopAccessError) as caught:
                _guard_transport()
        self.assertEqual(caught.exception.code, "desktop_token_required")

    def test_wrong_launch_token_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError
        from integrations.desktop.local_root import _guard_transport

        self.web.ctx.env = {"REMOTE_ADDR": "::1",
                            "HTTP_X_COW_DESKTOP_TOKEN": "nope"}
        with patch.dict(os.environ, {"COW_DESKTOP_TOKEN": DESKTOP_TOKEN}):
            with self.assertRaises(DesktopAccessError):
                _guard_transport()

    def test_loopback_with_the_matching_token_passes(self):
        from integrations.desktop.local_root import _guard_transport

        self.web.ctx.env = {"REMOTE_ADDR": "127.0.0.1",
                            "HTTP_X_COW_DESKTOP_TOKEN": DESKTOP_TOKEN}
        with patch.dict(os.environ, {"COW_DESKTOP_TOKEN": DESKTOP_TOKEN}):
            _guard_transport()  # must not raise


# ---------------------------------------------------------------------------
# Registration against a real identity store
# ---------------------------------------------------------------------------


class _LocalRootWebBase(unittest.TestCase):
    """Authorization of a registration, on a real app and a real DB.

    ``_guard_transport`` is exercised above with real ``web.ctx`` state; here it
    is stubbed out so the *authorization* logic (native-only, ownership, mode)
    is what is under test rather than the transport again.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-local-root-")
        cls.app = WebAppHarness(cls._tmp.name + "/instance")
        cls.app.add_agent(AGENT)
        cls.app.role("root-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:" + AGENT, "use")])
        cls.u1 = cls.app.member("root-u1", ["root-role"])
        cls.u2 = cls.app.member("root-u2", ["root-role"])
        from integrations.desktop import access as access_mod
        owners = {SESSION_A: cls.u1, SESSION_B: cls.u2}
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: owners.get(session_id))

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(None)
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        from agent.desktop_local import reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self._patch = patch(
            "auth.capability_matrix.slice_for",
            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._guard = patch(
            "integrations.desktop.local_root._guard_transport",
            lambda: None)
        self._guard.start()
        self.addCleanup(self._guard.stop)
        import secrets
        self.root = os.path.join(self._tmp.name,
                                 "proj-" + secrets.token_hex(4))
        os.makedirs(self.root, exist_ok=True)

    # -- fixtures -----------------------------------------------------------

    def native(self, username="root-u1"):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login(username)
        started = desktop.begin(
            session_token=web_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=web_token)
        return desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=ORIGIN)["token"]

    def paired_web(self, username="root-u1"):
        import secrets
        from auth.desktop_web_session import service_for
        native = self.native(username)
        child = service_for(self.app.service).bootstrap(
            native_token=native,
            bootstrap_id=secrets.token_urlsafe(18),
            instance_id=secrets.token_urlsafe(18),
            web_protocol=1, origin=ORIGIN)
        return native, child["web_token"]

    def devices(self):
        from integrations.desktop.devices import service_for
        return service_for(self.app.service)

    def service(self):
        from integrations.desktop.local_root import service_for
        return service_for(self.app.service)

    def full_scope(self, username="root-u1", session=SESSION_A, version=1,
                   mode=None):
        import secrets
        native, web = self.paired_web(username)
        device = self.devices().register_device(
            token=native,
            installation_id=("install_" + secrets.token_urlsafe(18))[:64],
            display_name="MacBook", platform="macos", client_version="2.1.9")
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id=AGENT, business_session_id=session, context_nonce=NONCE)
        kwargs = dict(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="销售台账", grant_version=version)
        if mode is not None:
            kwargs["project_mode"] = mode
        workspace = self.devices().register_workspace(**kwargs)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=version)
        return native, device, binding, workspace

    def register(self, native, device, binding, workspace, **overrides):
        kwargs = dict(
            token=native, binding_id=binding["id"], device_id=device["id"],
            workspace_id=workspace["id"], absolute_path=self.root)
        kwargs.update(overrides)
        return self.service().register_root(**kwargs)

    # -- tests --------------------------------------------------------------
    # (in ``RegisterRootTests`` below; the fixtures above are shared with the
    # session-binding cases)


class RegisterRootTests(_LocalRootWebBase):
    """Registering a resolved root with the backend."""

    def test_happy_path_registers_without_echoing_the_path(self):
        from agent.desktop_local import registry

        native, device, binding, workspace = self.full_scope()
        data = self.register(native, device, binding, workspace)
        self.assertTrue(data["registered"])
        self.assertEqual(data["project_mode"], "readonly-input")
        for forbidden in ("absolute_path", "path", "root"):
            self.assertNotIn(forbidden, data)
        # The backend can now resolve the session's target to the real root...
        entry = registry().lookup(
            user_id=self.u1, tenant_id=self.app.tenant_id,
            device_id=device["id"], workspace_id=workspace["id"],
            binding_id=binding["id"], grant_version=1)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.absolute_path, os.path.normpath(self.root))

    def test_workspace_reports_its_purpose(self):
        native, device, binding, workspace = self.full_scope()
        self.assertEqual(workspace["project_mode"], "readonly-input")
        # A pre-change row (no mode stored) reads as the weak purpose.
        rows = self.app.service._store.execute(
            "SELECT project_mode FROM desktop_workspaces WHERE id=?",
            (workspace["id"],))
        self.assertEqual(rows[0]["project_mode"], "readonly-input")

    def test_project_execution_is_refused_for_a_readonly_grant(self):
        """The transport cannot promote a file reference into execution."""
        from integrations.desktop.errors import DesktopAccessError
        from agent.desktop_local import registry

        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.register(native, device, binding, workspace,
                          project_mode="project-execution")
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(len(registry()), 0)

    def test_project_execution_is_allowed_when_the_grant_authorized_it(self):
        native, device, binding, workspace = self.full_scope(
            mode="project-execution")
        data = self.register(native, device, binding, workspace,
                             project_mode="project-execution")
        self.assertEqual(data["project_mode"], "project-execution")

    def test_unknown_mode_is_refused(self):
        from integrations.desktop.errors import DesktopAccessError

        native, device, binding, workspace = self.full_scope()
        for bad in ("read-write", "execution", "READONLY-INPUT"):
            with self.assertRaises(DesktopAccessError) as caught:
                self.register(native, device, binding, workspace,
                              project_mode=bad)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_another_users_workspace_is_hidden(self):
        from integrations.desktop.errors import DesktopAccessError
        from agent.desktop_local import registry

        native2, device2, binding2, workspace2 = self.full_scope(
            username="root-u2", session=SESSION_B)
        native1, device1, binding1, workspace1 = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.register(native1, device1, binding1, workspace2)
        self.assertEqual(caught.exception.code, "resource_not_found")
        self.assertEqual(len(registry()), 0)

    def test_a_web_child_token_cannot_register(self):
        """Only the native session may hand over a root it never picked."""
        from integrations.desktop.errors import DesktopAccessError
        from agent.desktop_local import registry

        native, web = self.paired_web()
        import secrets
        device = self.devices().register_device(
            token=native,
            installation_id=("install_" + secrets.token_urlsafe(18))[:64],
            display_name="MacBook", platform="macos", client_version="2.1.9")
        binding = self.devices().create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id=AGENT, business_session_id=SESSION_A, context_nonce=NONCE)
        workspace = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id,
            device_id=device["id"], label="台账", grant_version=1)
        self.devices().bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        with self.assertRaises(DesktopAccessError) as caught:
            self.service().register_root(
                token=web, binding_id=binding["id"], device_id=device["id"],
                workspace_id=workspace["id"], absolute_path=self.root)
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertEqual(len(registry()), 0)

    def test_a_relative_path_is_refused(self):
        from agent.desktop_local import LocalRootError

        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(LocalRootError):
            self.register(native, device, binding, workspace,
                          absolute_path="relative/project")

    def test_revoke_stops_resolution_and_is_idempotent(self):
        from agent.desktop_local import registry

        native, device, binding, workspace = self.full_scope()
        self.register(native, device, binding, workspace)
        self.assertEqual(len(registry()), 1)
        first = self.service().revoke_root(
            token=native, device_id=device["id"],
            workspace_id=workspace["id"])
        self.assertEqual(first["revoked"], 1)
        self.assertEqual(len(registry()), 0)
        again = self.service().revoke_root(
            token=native, device_id=device["id"],
            workspace_id=workspace["id"])
        self.assertEqual(again["revoked"], 0)


class SessionTargetTests(_LocalRootWebBase):
    """Binding a chat to a local project, on the same real app.

    The fixtures above are exactly what a session binding needs (a live native
    session owning a device/binding/workspace), so the session cases share them
    rather than carrying a second copy.
    """

    def setUp(self):
        # The project store outlives one test (it lives in the class-scoped
        # instance dir), so a target written by an earlier case would make the
        # "was it refused?" assertions pass vacuously.
        super().setUp()
        from agent.workspace import project_store
        from common.runtime_identity import RuntimeIdentity, use_identity

        identity = RuntimeIdentity(
            agent_id=AGENT, user_id=self.u1, tenant_id=self.app.tenant_id,
            session_id=SESSION_A)
        with use_identity(identity):
            project_store.clear_execution_target(SESSION_A, AGENT)

    def bind(self, native, device, binding, workspace, **overrides):
        kwargs = dict(
            token=native, agent_id=AGENT, session_id=SESSION_A,
            binding_id=binding["id"], device_id=device["id"],
            workspace_id=workspace["id"])
        kwargs.update(overrides)
        return self.service().bind_session_target(**kwargs)

    def stored(self, session_id=SESSION_A):
        from agent.workspace import project_store
        from common.runtime_identity import RuntimeIdentity, use_identity

        identity = RuntimeIdentity(
            agent_id=AGENT, user_id=self.u1, tenant_id=self.app.tenant_id,
            session_id=session_id)
        with use_identity(identity):
            return project_store.get_execution_target(session_id, AGENT)

    def test_bind_persists_the_workspace_grant_version(self):
        native, device, binding, workspace = self.full_scope()
        self.bind(native, device, binding, workspace)
        target = self.stored()
        self.assertIsNotNone(target)
        self.assertTrue(target.is_desktop)
        # Version comes from the workspace row, not from the caller.
        self.assertEqual(target.grant_version, workspace["grant_version"])
        self.assertEqual(target.workspace_id, workspace["id"])
        self.assertEqual(target.binding_id, binding["id"])

    def test_the_persisted_target_holds_no_path(self):
        import json
        from common import state_dir
        from common.runtime_identity import RuntimeIdentity

        native, device, binding, workspace = self.full_scope()
        self.register(native, device, binding, workspace)
        self.bind(native, device, binding, workspace)
        identity = RuntimeIdentity(
            agent_id=AGENT, user_id=self.u1, tenant_id=self.app.tenant_id)
        path = state_dir.user_root(identity) / "projects.json"
        raw = path.read_text(encoding="utf-8")
        self.assertIn("workspace_id", raw)
        for forbidden in ("absolute_path", "/Users/", self.root):
            self.assertNotIn(forbidden, raw)
        json.loads(raw)  # still a well-formed store

    def test_bind_refuses_another_users_conversation(self):
        from integrations.desktop.errors import DesktopAccessError

        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.bind(native, device, binding, workspace, session_id=SESSION_B)
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertIsNone(self.stored())

    def test_bind_refuses_project_execution_on_a_readonly_grant(self):
        from integrations.desktop.errors import DesktopAccessError

        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.bind(native, device, binding, workspace,
                      project_mode="project-execution")
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertIsNone(self.stored())

    def test_bind_carries_the_workspace_mode_by_default(self):
        native, device, binding, workspace = self.full_scope(
            mode="project-execution")
        self.bind(native, device, binding, workspace)
        target = self.stored()
        self.assertEqual(target.project_mode, "project-execution")
        self.assertTrue(target.allows_project_execution)

    def test_bind_refuses_an_unlinked_workspace(self):
        """A workspace that exists but was never bound cannot be adopted."""
        from integrations.desktop.errors import DesktopAccessError

        native, device, binding, workspace = self.full_scope()
        other = self.devices().register_workspace(
            token=native, tenant_id=self.app.tenant_id, device_id=device["id"],
            label="另一个", grant_version=1)
        with self.assertRaises(DesktopAccessError) as caught:
            self.bind(native, device, binding, other)
        self.assertEqual(caught.exception.code, "stale_context")
        self.assertIsNone(self.stored())

    def test_bind_refuses_a_relinked_binding_for_another_native_session(self):
        from integrations.desktop.errors import DesktopAccessError

        native2, device2, binding2, workspace2 = self.full_scope(
            username="root-u2", session=SESSION_B)
        native1, device1, binding1, workspace1 = self.full_scope()
        with self.assertRaises(DesktopAccessError):
            self.bind(native1, device1, binding1, workspace2)
        self.assertIsNone(self.stored())

    def test_clear_is_idempotent(self):
        native, device, binding, workspace = self.full_scope()
        self.bind(native, device, binding, workspace)
        self.assertIsNotNone(self.stored())
        first = self.service().clear_session_target(
            token=native, agent_id=AGENT, session_id=SESSION_A,
            tenant_id=self.app.tenant_id)
        self.assertTrue(first["cleared"])
        self.assertIsNone(self.stored())
        again = self.service().clear_session_target(
            token=native, agent_id=AGENT, session_id=SESSION_A,
            tenant_id=self.app.tenant_id)
        self.assertTrue(again["cleared"])

    def test_a_revoked_workspace_link_stops_being_bindable(self):
        from integrations.desktop.errors import DesktopAccessError

        native, device, binding, workspace = self.full_scope()
        self.devices().revoke_workspace(
            token=native, tenant_id=self.app.tenant_id,
            workspace_id=workspace["id"])
        with self.assertRaises(DesktopAccessError) as caught:
            self.bind(native, device, binding, workspace)
        self.assertEqual(caught.exception.code, "grant_revoked")
        self.assertIsNone(self.stored())

    def test_tenant_is_required_and_must_not_conflict(self):
        from integrations.desktop.errors import DesktopAccessError

        native, device, binding, workspace = self.full_scope()
        with self.assertRaises(DesktopAccessError) as caught:
            self.bind(native, device, binding, workspace, tenant_id="tnt_other")
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertIsNone(self.stored())


if __name__ == "__main__":
    unittest.main()
