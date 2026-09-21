# encoding:utf-8
"""Upstream-drift guards (task 10.5).

Every obligation in this change survives only as long as a future ``master``
update cannot slip past it unnoticed. The merge itself is a text problem; these
are the *semantic* drifts a clean merge hides, and each one here is expressed as
a case someone can re-run after any upstream sync:

1. an upstream **new HTTP method** on an already-recovered URL cannot appear
   without a policy: appending a second entry for the same pattern is refused,
   extending an entry with a method whose policy is unknown is refused, and a
   method implemented by the handler but left out of the registry is reported by
   the coverage invariant;
2. an upstream **new task field** (top level or inside ``action``) is preserved
   through a real console edit, while the protected identity fields stay
   un-forgeable — "new functionality kept" and "still authorizes the same way"
   in one case.

The remaining drifts listed in the task (a new Desktop transport, a new memory
index/publish entry, a new channel action dispatch) are covered by their own
suites: ``tests/test_desktop_tenant_context*.py``, ``tests/test_memory_console.py``
and the channel seams, because each of them needs that subsystem's fixtures
rather than the route registry's.
"""

import hashlib
import json
import re
import unittest
from pathlib import Path

from channel.web.route_registry import (
    CoverageViolation,
    RouteEntry,
    check_route_coverage,
    derive_route_policy,
    _validate_entry,
)
from tests._helpers import WebAppHarness

AGENT = "shared-agent"


class RegistryDriftTests(unittest.TestCase):
    """A new HTTP method must be classified, not appended."""

    def test_appending_a_second_entry_for_a_recovered_url_is_refused(self):
        """The naive "add the new method as another route" edit cannot land."""
        entries = [
            RouteEntry("/api/scheduler", "SchedulerHandler", "upstream",
                       {"GET": {"policy": "tenant"}}),
            # The upstream sync adds PUT to the same URL as a second entry.
            RouteEntry("/api/scheduler", "SchedulerHandler", "upstream",
                       {"PUT": {"policy": "tenant"}}),
        ]
        with self.assertRaises(ValueError) as error:
            derive_route_policy(entries)
        self.assertIn("duplicate route pattern", str(error.exception))

    def test_a_new_method_without_a_policy_is_refused(self):
        """An upstream method added with no/unknown policy fails validation."""
        for methods in ({"PUT": {}}, {"PUT": {"policy": "whatever"}},
                        {"TRACE": {"policy": "tenant"}}):
            entry = RouteEntry("/api/scheduler", "SchedulerHandler", "upstream",
                               methods)
            with self.assertRaises(CoverageViolation, msg=methods):
                _validate_entry(entry)

    def test_a_handler_method_missing_from_the_registry_is_reported(self):
        """An upstream method on a recovered handler blocks acceptance until
        it is registered with a policy."""

        class _Drifted:
            def GET(self):  # noqa: N802 - upstream handler shape
                return "ok"

            def PUT(self):  # noqa: N802 - the new upstream method
                return "ok"

        entries = [RouteEntry("/api/scheduler", "_Drifted", "upstream",
                              {"GET": {"policy": "tenant"}})]
        violations = check_route_coverage({"_Drifted": _Drifted}, entries)
        self.assertTrue(any("PUT" in v for v in violations), violations)

    def test_the_real_registry_is_currently_classified(self):
        """The invariant the two probes above protect, on the real table."""
        import channel.web.web_channel as web_channel

        self.assertEqual(check_route_coverage(vars(web_channel)), [])


class TaskFieldDriftTests(unittest.TestCase):
    """An upstream task field survives preservation *and* authorization."""

    @classmethod
    def setUpClass(cls):
        cls.app = WebAppHarness(root=cls._root())
        cls.app.add_agent(AGENT)
        role = cls.app.role("console-member", ["chat.use", "agent.use", "agent.read"])
        cls.alice = cls.app.member("alice", [role["code"]])
        cls.token = cls.app.login("alice")

    @classmethod
    def _root(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory(prefix="drift-guard-")
        return cls._tmp.name

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def _task_with_upstream_fields(self, task_id="t-drift"):
        store = self.app.scheduler_store(AGENT)
        self.app.personal_task(
            AGENT, self.alice, id=task_id, name=task_id,
            # Two shapes of "upstream added a field": one at the task level, one
            # inside the action the scheduler hands to the Agent.
            upstream_trace={"origin": "master", "revision": 7},
            action={"type": "message", "content": "ping", "channel_type": "web",
                    "receiver": self.alice,
                    "upstream_action_field": {"nested": True}})
        return store

    def test_a_new_upstream_field_survives_an_edit(self):
        store = self._task_with_upstream_fields("t-keep")
        response = self.app.post("/api/scheduler/update",
                                 {"task_id": "t-keep", "agent_id": AGENT,
                                  "name": "renamed"}, token=self.token)
        body = json.loads(response.data.decode("utf-8"))
        self.assertEqual(body["status"], "success", body)
        task = store.get_task("t-keep")
        self.assertEqual(task["name"], "renamed")
        self.assertEqual(task.get("upstream_trace"),
                         {"origin": "master", "revision": 7})
        self.assertEqual((task.get("action") or {}).get("upstream_action_field"),
                         {"nested": True})

    def test_an_edit_still_cannot_forge_the_protected_identity(self):
        store = self._task_with_upstream_fields("t-forge")
        for patch in ({"owner": {"user_id": "someone-else"}},
                      {"tenant_id": "other-tenant"},
                      {"action": {"receiver": "someone-else"}}):
            body = dict(patch)
            body.update({"task_id": "t-forge", "agent_id": AGENT})
            response = self.app.post("/api/scheduler/update", body, token=self.token)
            payload = json.loads(response.data.decode("utf-8"))
            self.assertEqual(payload["status"], "error", (patch, payload))
            self.assertEqual(payload["code"], "forged_field", (patch, payload))
        task = store.get_task("t-forge")
        self.assertEqual((task.get("owner") or {}).get("user_id"), self.alice)
        self.assertEqual((task.get("action") or {}).get("receiver"), self.alice)


class TasksPagePortDriftTests(unittest.TestCase):
    """The ported scheduled-task page is upstream's: drift is re-ported by hand.

    The page is assembled from five upstream files served byte-for-byte
    (``port-upstream-tasks-page``) and one fork module that re-implements the
    parts upstream has no notion of. Neither half survives an upstream sync on
    its own: if upstream edits a fragment, the markup the fork's module reasons
    about changed; if upstream edits a script, the functions the fork captured
    changed. Both are silent failures, so both are pinned here.
    """

    ROOT = Path(__file__).resolve().parents[1] / "channel" / "web"

    # path -> sha256 of the upstream revision this port was taken from. These
    # are *meant* to need a hand edit: an upstream change to any of them must
    # send whoever merges it back through the fork module, not past it.
    UPSTREAM_SOURCES = {
        "templates/views/tasks.html":
            "a765146637966955c90dfc84afa55df67b9201de33d7a38b773ec564b7805e98",
        "templates/modals/task-edit.html":
            "c6cf202966564368b3f33f85ba33b18b9784a0da8d17e88607e5b6130e6de939",
        "templates/modals/run-detail.html":
            "f28e16865f2839da38d16599d3ecd3dfe62b27a52904179053662fdd43b028ea",
        "static/js/views/tasks.js":
            "f3d2263e0593ec3c1bb73300ab61804ed81f8e822d54a12c58af27277e254431",
        "static/js/views/tasks-modal.js":
            "e7219f32fc9dd44fdea6cc602a03c1e52dbdfdd63c8a91fcd36761c5f4492d37",
    }
    PARALLEL_FORK_MODULE = "static/js/fork/tasks-console.js"

    def _hash(self, relative):
        return hashlib.sha256((self.ROOT / relative).read_bytes()).hexdigest()

    def test_the_registered_upstream_sources_are_unchanged(self):
        """A byte changed upstream -> re-port the fork module by hand."""
        drifted = [relative for relative, digest in self.UPSTREAM_SOURCES.items()
                   if self._hash(relative) != digest]
        self.assertEqual(
            drifted, [],
            "upstream scheduled-task sources changed: re-port "
            f"{self.PARALLEL_FORK_MODULE} against them, then update the digests "
            "in TasksPagePortDriftTests.UPSTREAM_SOURCES (drifted: "
            f"{', '.join(drifted)})")

    def test_the_parallel_module_lives_outside_the_upstream_tree(self):
        """fork-upstream-decoupling: the fork's copy is not an upstream file."""
        module = self.ROOT / self.PARALLEL_FORK_MODULE
        self.assertTrue(module.is_file(), module)
        upstream_dirs = ["static/js/core/", "static/js/chat/", "static/js/views/"]
        self.assertFalse(
            any(self.PARALLEL_FORK_MODULE.startswith(prefix) for prefix in upstream_dirs),
            self.PARALLEL_FORK_MODULE)

    def test_the_parallel_module_names_the_upstream_sources_it_carries(self):
        """The registration has to be readable from the module itself."""
        header = (self.ROOT / self.PARALLEL_FORK_MODULE).read_text(encoding="utf-8")
        header = header[:header.index("(function ()")]
        missing = [relative for relative in self.UPSTREAM_SOURCES
                   if relative not in header]
        self.assertEqual(missing, [],
                         f"{self.PARALLEL_FORK_MODULE} does not name {missing} "
                         "in its header")

    def _top_level_declarations(self, source):
        """Names a classic script would put in the page's shared global scope."""
        names = set()
        patterns = (
            r"(?m)^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
            r"(?m)^(?:let|const|var)\s+([A-Za-z_$][\w$]*)",
            r"(?m)^class\s+([A-Za-z_$][\w$]*)",
        )
        for pattern in patterns:
            names.update(re.findall(pattern, source))
        return names

    def test_no_upstream_module_shadow_would_silently_win(self):
        """Two scripts declaring one name: either a page-killing SyntaxError
        (``let``/``const``) or a silent shadow ("which one am I calling?").

        Every script the page loads shares one global scope, so the intersection
        between the upstream scheduled-task scripts and the rest of the page
        must stay empty.
        """
        upstream = set()
        for relative in self.UPSTREAM_SOURCES:
            if relative.endswith(".js"):
                upstream |= self._top_level_declarations(
                    (self.ROOT / relative).read_text(encoding="utf-8"))
        self.assertTrue(upstream, "no upstream script was read")

        html = (self.ROOT / "chat.html").read_text(encoding="utf-8")
        loaded = [f"static/{asset}" for asset
                  in re.findall(r'<script defer src="assets/(js/[^"]+)"></script>', html)]
        self.assertIn(self.PARALLEL_FORK_MODULE, loaded, "the patch module is not loaded")

        collisions = {}
        for relative in loaded:
            if relative in self.UPSTREAM_SOURCES:
                continue
            names = self._top_level_declarations(
                (self.ROOT / relative).read_text(encoding="utf-8"))
            shared = sorted(names & upstream)
            if shared:
                collisions[relative] = shared
        self.assertEqual(
            collisions, {},
            "these scripts declare the same top-level names as the upstream "
            f"scheduled-task scripts: {collisions}. In one shared scope a "
            "`let`/`const` pair is a page-killing SyntaxError and a `function` "
            "pair silently shadows whichever loaded first")

    def test_the_patch_module_declares_nothing_at_the_top_level(self):
        """It installs by assigning window properties from inside one IIFE; a
        top-level name here would be the collision above, by construction."""
        names = self._top_level_declarations(
            (self.ROOT / self.PARALLEL_FORK_MODULE).read_text(encoding="utf-8"))
        self.assertEqual(names, set(), f"{self.PARALLEL_FORK_MODULE} declares top-level names")

    def test_the_patch_module_loads_after_both_halves_it_overrides(self):
        """It captures upstream's functions and wraps console.js's initDropdown,
        so a page that loads it first leaves it with nothing to capture (its own
        guard then bows out and the fork semantics never install)."""
        html = (self.ROOT / "chat.html").read_text(encoding="utf-8")
        order = re.findall(r'<script defer src="assets/(js/[^"]+)"></script>', html)
        patch = order.index("js/fork/tasks-console.js")
        for relative in ("js/views/tasks.js", "js/views/tasks-modal.js",
                         "js/console.js"):
            self.assertIn(relative, order, f"{relative} is not loaded")
            self.assertLess(order.index(relative), patch,
                            f"{relative} must be evaluated before the patch module")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()