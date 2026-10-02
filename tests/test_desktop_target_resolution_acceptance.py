# encoding:utf-8
"""A03 / A08 / A10 / A30 / A31 -- target resolution and its authorization.

Change ``align-desktop-project-execution-with-master`` (task 3.8).

Each class below is one acceptance row, and each test drives the *real* seam the
row is about -- the resolver, the run context, the authorization gate, the
incoming-command contract -- rather than a mock of it. Rows whose remaining
branches are already pinned by a neighbour suite say so in the docstring, so the
evidence document can cite both instead of duplicating the assertions here.

The property every row shares: a reference to a local project is a *target*, and
every branch that turns a target into work re-checks authorization at the moment
of use. Forging the reference, reusing someone else's selection, addressing the
wrong Agent and triggering outside a user's turn all have to end in a refusal
that says so -- never in a fallback to a server directory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

#: The ops a page may ask a paired device to run (contracts/desktop/v1.json).
#: Deliberately asserted as an exact set: widening this surface is the change a
#: reviewer must see, and it must never grow a generic shell/exec entry.
DEVICE_OPS = {"list", "stat", "search", "read_text", "materialize", "inspect"}

#: Substrings that would name a generic command channel if one ever appeared.
GENERIC_EXEC_MARKERS = ("exec", "shell", "spawn", "module", "eval", "command_run")


class _AcceptanceCase(unittest.TestCase):
    """A trusted same-machine registry with one live project per user."""

    def setUp(self):
        from agent.desktop_local import LocalRootRegistry, reset_registry
        from agent.registry import AgentRegistry, set_agent_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self._registry_patch = patch("agent.desktop_local.registry",
                                     return_value=self.registry)
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="target-resolution-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj-a")
        self.other_root = os.path.join(self._tmp.name, "proj-b")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.other_root, exist_ok=True)
        # A real deployment always has the addressed Agent configured; the gate
        # refuses an unregistered one, which is *not* what these rows test.
        self.set_agents("host")

    def set_agents(self, *profiles):
        """Install a registry over ``profiles`` (ids or ``AgentProfile``s)."""
        from agent.registry import AgentProfile, AgentRegistry, set_agent_registry

        resolved = [item if isinstance(item, AgentProfile) else self._profile(item)
                    for item in profiles]
        set_agent_registry(AgentRegistry(resolved, default_agent_id=resolved[0].id))
        self.addCleanup(lambda: set_agent_registry(None))

    def _profile(self, agent_id, **overrides):
        from agent.registry import AgentProfile

        fields = {"name": agent_id.title(),
                  "workspace": os.path.join(self._tmp.name, agent_id)}
        fields.update(overrides)
        return AgentProfile(agent_id, **fields)

    def target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "d1", "workspace_id": "w1", "binding_id": "b1",
                  "grant_version": 1}
        fields.update(overrides)
        return desktop_target(**fields)

    def register(self, **overrides):
        fields = dict(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1, project_mode="readonly-input",
            absolute_path=self.root)
        fields.update(overrides)
        return self.registry.register(**fields)

    def identity(self, frozen=None, root=None, **target_overrides):
        """A run identity carrying ``target_overrides`` as its frozen target."""
        from common.runtime_identity import RuntimeIdentity

        target = self.target(**target_overrides)
        if frozen is None:
            entry = self.registry.lookup(
                user_id="u1", tenant_id="t1", device_id=target.device_id,
                workspace_id=target.workspace_id, binding_id=target.binding_id,
                grant_version=target.grant_version,
                require_mode=target.project_mode)
            frozen = entry.absolute_path if entry else ""
        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target, execution_cwd=frozen)


class A03ForgedReferenceTests(_AcceptanceCase):
    """A03: a page cannot name a host path, and cannot reuse a foreign selection.

    The transport half of the row (off-loopback / missing or wrong launch token /
    a Web-child bearer) is driven over real HTTP in
    ``tests/test_desktop_local_root.py::TransportGuardTests`` and
    ``::RegisterRootTests``; what is added here is the resolution half -- what
    happens when a *reference* carrying a path reaches the local source.
    """

    def test_a_page_supplied_absolute_path_is_refused_not_parsed(self):
        from agent.desktop_local.source_resolver import (
            REFUSAL_SERVER_PATH, resolve_reference, source_for_session,
        )

        self.register()
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()):
            source = source_for_session("s1", "host", identity=self.identity())
        self.assertTrue(source.available)

        resolved = resolve_reference(source, "/etc/passwd")
        self.assertIsNone(resolved.absolute)
        self.assertEqual(resolved.refusal, REFUSAL_SERVER_PATH.format(raw="/etc/passwd"))
        # The server's own file surface was never consulted for a local project.
        self.assertNotEqual(resolved.kind, "server")

    def test_another_users_selection_record_is_not_usable_by_id(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        # The same binding/workspace ids, but the live root belongs to u2.
        self.register(user_id="u2")
        run = self.identity()  # u1, same identifiers
        self.assertEqual(run_local_cwd(run), (None, REFUSAL_UNAVAILABLE))
        self.assertIsNone(self.registry.lookup(
            user_id="u1", tenant_id="t1", device_id="d1", workspace_id="w1",
            binding_id="b1", grant_version=1))

    def test_a_handle_from_before_a_repick_does_not_resolve(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register(grant_version=1)
        stale = self.identity()
        self.register(grant_version=2, absolute_path=self.other_root)
        self.assertEqual(run_local_cwd(stale), (None, REFUSAL_UNAVAILABLE))

    def test_a_handle_does_not_follow_a_directory_swapped_under_it(self):
        """Same identifiers and version, different path: the frozen copy wins.

        Without the frozen-vs-live comparison this is exactly the case that
        silently re-points a run at a directory its authorization was never
        issued for.
        """
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd

        self.register()
        stale = self.identity()
        self.assertEqual(run_local_cwd(stale), (self.root, None))
        self.register(absolute_path=self.other_root)  # same version
        self.assertEqual(run_local_cwd(stale), (None, REFUSAL_UNAVAILABLE))

    def test_an_escaped_relative_id_is_refused(self):
        from agent.desktop_local.source_resolver import (
            REFUSAL_ESCAPES, REFUSAL_SERVER_PATH, resolve_reference,
        )

        self.register()
        source = self.source()
        for raw, expected in (("../outside.txt", REFUSAL_ESCAPES),
                              ("a/../../outside.txt", REFUSAL_ESCAPES),
                              ("~/.ssh/id_rsa", REFUSAL_SERVER_PATH.format(raw="~/.ssh/id_rsa"))):
            with self.subTest(raw=raw):
                resolved = resolve_reference(source, raw)
                self.assertIsNone(resolved.absolute)
                self.assertEqual(resolved.refusal, expected)
                # Every one of them says the server directory was not used.
                self.assertIn("服务器", resolved.refusal)

    def test_the_personal_projects_root_rule_is_untouched(self):
        """The server-side guard a local project must not widen (A03's second half)."""
        from agent.workspace import project_store

        with patch.object(project_store, "user_projects_root", return_value=self._tmp.name):
            project_store._require_within_user_root(self.root)  # inside: allowed
            with self.assertRaises(ValueError):
                project_store._require_within_user_root("/tmp/definitely-outside")

    def source(self, **target_overrides):
        from agent.desktop_local.source_resolver import source_for_identity

        return source_for_identity(self.identity(**target_overrides))


class A08CachedAgentSwapTests(_AcceptanceCase):
    """A08: two conversations share one cached Agent; neither moves the other.

    The tool-view half of the row is driven with real tool objects in
    ``tests/test_desktop_run_context.py::ToolViewTests``; this class drives the
    *session* half -- entry resolution for two sessions over one shared Agent,
    including a re-point while the other run is still in flight.
    """

    class _Agent:
        """The shared, mutable object both turns write to."""

        def __init__(self):
            self.project_dir = "/server/workspace"

        def apply_project_dir(self, project_dir, scope=None):
            self.project_dir = project_dir
            return project_dir

    def _targets(self, by_session):
        def _get(session_id, agent_id=None):
            return by_session.get(session_id)
        return patch("agent.workspace.project_store.get_execution_target", _get)

    def test_each_run_gets_its_own_directory_and_card_scope(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from agent.desktop_local.run_context import local_run_scope
        from common.runtime_identity import RuntimeIdentity, use_identity

        self.register()  # A: workspace w1 -> self.root
        self.register(workspace_id="w2", binding_id="b2", absolute_path=self.other_root)
        targets = {"sA": self.target(), "sB": self.target(workspace_id="w2", binding_id="b2")}
        agent = self._Agent()

        with self._targets(targets), use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            with execution_target_scope("host", "sA") as run_a:
                cwd_a = run_a.execution_cwd
                scope_a = local_run_scope(run_a)
                # A concurrent turn in the other conversation re-points the
                # shared Agent -- the in-flight run's answer must not move.
                agent.apply_project_dir(self.other_root)
                with execution_target_scope("host", "sB") as run_b:
                    self.assertEqual(run_b.execution_cwd, self.other_root)
                    self.assertEqual(scope_a, local_run_scope(run_a))
                    self.assertEqual(local_run_scope(run_b)["workspace_id"], "w2")
                self.assertEqual(run_a.execution_cwd, cwd_a)

    def test_repointing_moves_the_next_turn_only(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, use_identity

        self.register()
        self.register(workspace_id="w2", binding_id="b2", absolute_path=self.other_root)
        stored = {"s1": self.target()}

        with self._targets(stored), use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            with execution_target_scope("host", "s1") as first:
                self.assertEqual(first.execution_cwd, self.root)
                stored["s1"] = self.target(workspace_id="w2", binding_id="b2")
                self.assertEqual(first.execution_cwd, self.root)
            with execution_target_scope("host", "s1") as second:
                self.assertEqual(second.execution_cwd, self.other_root)


class A10TeamHandoffTests(_AcceptanceCase):
    """A10: the *actual* executing Agent's eligibility decides.

    The eligibility predicate itself (allow/deny, skill selection, disabled and
    unknown Agents) is covered in
    ``tests/test_desktop_run_authorization.py::AgentLocalEligibilityTests``; this
    class drives the consequence on a live grant, and the proxy-tool half.
    """

    def test_a_teammate_without_project_tools_keeps_the_grant_refused(self):
        from agent.desktop_local.run_authorization import (
            REFUSAL_NEEDS_INTERACTIVE, authorize_local_run, narrow_local_execution,
        )
        from agent.desktop_local.run_context import needs_local_directory, run_local_cwd
        from common.runtime_identity import current_identity, restore_identity, use_identity

        self.set_agents("host", self._profile("gated", tools_allowlist=["memory_search"]))
        self.register()
        allowed, refusal = authorize_local_run(host_agent_id="host",
                                              speaker_agent_id="gated")
        self.assertFalse(allowed)
        # The refusal is about the *Agent*, not the turn: the same turn sent by
        # the host is allowed, and the grant itself is live.
        self.assertNotEqual(refusal, REFUSAL_NEEDS_INTERACTIVE)
        self.assertIn("gated", refusal)
        self.assertEqual(authorize_local_run(host_agent_id="host"), (True, None))
        self.assertFalse(needs_local_directory(object(), "memory_search"))

        with use_identity(self.identity()):
            self.assertEqual(run_local_cwd(current_identity())[0], self.root)
            token = narrow_local_execution(refusal)
            try:
                narrowed = current_identity()
                self.assertEqual(run_local_cwd(narrowed)[0], None)
                self.assertIn("gated", run_local_cwd(narrowed)[1])
            finally:
                restore_identity(token)
            # The narrowing was this run's only; the session's grant survives.
            self.assertEqual(run_local_cwd(current_identity())[0], self.root)

    def test_the_local_file_proxy_obeys_the_same_tool_gate(self):
        """A blacklisted proxy tool is refused by the shared gate, not by name."""
        from agent.effective_capabilities import is_tool_allowed

        gated = self._profile("gated", tools_allowlist=["memory_search"])
        effective = _effective(gated)
        # The proxy that can reach local files is not a side door: it is denied
        # by the same resolution that denies every other tool.
        self.assertFalse(is_tool_allowed("client_files", effective))
        self.assertFalse(is_tool_allowed("read", effective))
        self.assertTrue(is_tool_allowed("memory_search", effective))

    def test_an_agent_with_project_tools_is_unchanged(self):
        from agent.desktop_local.run_authorization import authorize_local_run
        from agent.desktop_local.run_context import run_local_cwd

        self.set_agents("host", self._profile("peer", tools_allowlist=["read"]))
        self.register()
        self.assertEqual(authorize_local_run(host_agent_id="host",
                                            speaker_agent_id="peer"), (True, None))
        self.assertEqual(run_local_cwd(self.identity()), (self.root, None))


class A30NoProjectCompatibilityTests(_AcceptanceCase):
    """A30: no project, read-only input and a closed project keep working.

    Nothing here is refused for the wrong reason: a session without a local
    target is *not* a refusal, a read-only grant cannot be upgraded into
    execution, and closing the project returns the session to the server rules.
    """

    def test_a_session_without_a_project_is_not_a_refusal(self):
        from agent.desktop_local.run_authorization import authorize_local_run
        from agent.desktop_local.run_context import run_local_cwd
        from agent.desktop_local.source_resolver import source_for_identity
        from common.runtime_identity import RuntimeIdentity

        identity = RuntimeIdentity(user_id="u1", tenant_id="t1")
        self.assertEqual(run_local_cwd(identity), (None, None))
        self.assertEqual(authorize_local_run(host_agent_id="host"), (True, None))
        source = source_for_identity(identity, server_root="/srv/projects")
        self.assertEqual(source.kind, "server")
        self.assertEqual(source.root, "/srv/projects")

    def test_entering_a_local_scope_does_not_change_tool_policy(self):
        """Recallable through the following suite: shared-asset limits stay."""
        from channel.web.fork.execution_scope import execution_target_scope
        from agent.effective_capabilities import is_tool_allowed
        from common.runtime_identity import RuntimeIdentity, use_identity

        self.register()
        profile = self._profile("host", tools_denylist=["knowledge_write"])
        effective = _effective(profile)
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self.target()), \
                use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            before = is_tool_allowed("knowledge_write", effective)
            with execution_target_scope("host", "s1"):
                during = is_tool_allowed("knowledge_write", effective)
            after = is_tool_allowed("knowledge_write", effective)
        self.assertEqual((before, during, after), (False, False, False))

    def test_a_readonly_grant_cannot_be_read_as_execution(self):
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd
        from agent.workspace.execution_target import MODE_PROJECT_EXECUTION

        self.register(project_mode="readonly-input")
        # The stored grant is what makes a target executable; a session that
        # *claims* execution against a read-only grant resolves to nothing.
        self.assertFalse(self.identity().allows_project_execution())
        claiming = self.identity(project_mode=MODE_PROJECT_EXECUTION)
        self.assertEqual(run_local_cwd(claiming), (None, REFUSAL_UNAVAILABLE))

    def test_closing_the_project_returns_the_session_to_the_default(self):
        from channel.web.fork.execution_scope import execution_target_scope
        from common.runtime_identity import RuntimeIdentity, current_identity, use_identity

        self.register()
        stored = {"s1": self.target()}

        def _get(session_id, agent_id=None):
            return stored.get(session_id)

        with patch("agent.workspace.project_store.get_execution_target", _get), \
                use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            with execution_target_scope("host", "s1") as open_run:
                self.assertEqual(open_run.execution_cwd, self.root)
            stored["s1"] = None  # the user closed the project
            before = current_identity()
            with execution_target_scope("host", "s1"):
                # A no-op scope, not a refusal: the session is a plain server one.
                self.assertEqual(current_identity(), before)


class A31NoBackgroundGrantTests(_AcceptanceCase):
    """A31: no background subject inherits a local project, and no page exec IPC.

    The reply-path half (refusal happens before the cached Agent is touched, and
    the instance is detached) is
    ``tests/test_desktop_run_authorization.py::AgentReplyGateTests``; here the
    same fact is asserted on a *live* grant, plus the incoming-command surface.
    """

    def test_the_same_turn_is_allowed_only_when_a_user_sent_it(self):
        from agent.desktop_local.run_authorization import (
            REFUSAL_NEEDS_INTERACTIVE, authorize_local_run,
        )

        self.register()
        self.assertEqual(authorize_local_run(host_agent_id="host"), (True, None))
        for kwargs in ({"scheduled": True}, {"background": True},
                       {"machine_subject": True}, {"task_source": "scheduler"}):
            with self.subTest(**kwargs):
                self.assertEqual(
                    authorize_local_run(host_agent_id="host", **kwargs),
                    (False, REFUSAL_NEEDS_INTERACTIVE))

    def test_a_background_run_is_refused_by_the_run_context_too(self):
        from agent.desktop_local.run_authorization import (
            authorize_local_run, narrow_local_execution,
        )
        from agent.desktop_local.run_context import run_local_cwd
        from common.runtime_identity import current_identity, restore_identity, use_identity

        self.register()
        _allowed, refusal = authorize_local_run(host_agent_id="host", background=True)
        with use_identity(self.identity()):
            self.assertEqual(run_local_cwd(current_identity()), (self.root, None))
            token = narrow_local_execution(refusal)
            try:
                self.assertEqual(run_local_cwd(current_identity()), (None, refusal))
            finally:
                restore_identity(token)
            self.assertEqual(run_local_cwd(current_identity()), (self.root, None))

    def test_a_page_cannot_hand_the_bridge_a_shell_or_a_module(self):
        from auth.desktop_contracts import COMMANDS, validate_command_frame

        self.assertEqual(set(COMMANDS["ops"]), DEVICE_OPS)
        for op in COMMANDS["ops"]:
            with self.subTest(op=op):
                self.assertFalse([m for m in GENERIC_EXEC_MARKERS if m in op], op)

        base = {"v": 1, "type": "command", "request_id": "r",
                "connection_epoch": "e", "binding_id": "b", "workspace_id": "w",
                "grant_version": 1, "deadline": 1, "params_sha256": "x"}
        for op, params in (("exec", {"cmd": "rm -rf /"}),
                           ("shell", {"cmd": "id"}),
                           ("read_text", {"relative_path": "a.txt", "command": "id"})):
            with self.subTest(op=op):
                problems = validate_command_frame(dict(base, op=op, params=params))
                self.assertTrue(problems, (op, params))

    def test_the_preload_surface_has_no_command_channel(self):
        """The renderer's bridge exposes no channel that takes a command."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "desktop", "src", "main", "preload.ts")
        text = open(path, encoding="utf-8").read()
        exposed = text.split("exposeInMainWorld", 1)[1]
        offenders = [marker for marker in GENERIC_EXEC_MARKERS
                     if f"{marker}:" in exposed or f"'{marker}" in exposed]
        self.assertEqual(offenders, [], offenders)


# ---------------------------------------------------------------------------
# helpers kept module-level so the test classes stay readable
# ---------------------------------------------------------------------------

def _effective(profile):
    from agent.effective_capabilities import resolve_effective_capabilities

    return resolve_effective_capabilities(profile, scene=None)


if __name__ == "__main__":
    unittest.main()
