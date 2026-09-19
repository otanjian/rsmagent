# encoding:utf-8
"""Per-action capability projection
(change ``integrate-upstream-core-capabilities``, P1).

``/auth/context.feature_actions`` is the one place a client learns whether a
feature is *served*, and the route gate reads the same finalized slice. These
tests pin the invariants a future edit could silently break:

1. the eight public keys exist and are the only keys the projection reports;
2. an action is in the **same** state in both projections -- the JSON and the
   HTTP policy -- so "projection says open, route answers 503" (and the reverse)
   is impossible;
3. ``RDAI_DISABLED_ACTIONS`` can only narrow, never widen, and an unknown key is
   a startup configuration error rather than a silent no-op.

The original five ``scheduler`` management actions are asserted to stay open:
the new scheduler slices share that consumer, and a closed sibling must not turn
the consumer (or the existing routes) off.
"""

import json
import unittest

from auth import capability_matrix

#: Batch state after task 8.5, written as the *expectation* rather than read back
#: from the registry -- a test that derives it from the declaration would agree
#: with any declaration, including a wrong one. The R2 batch (scheduler targets,
#: create and history) is accepted by the real-channel acceptance of tasks
#: 6.7 / 8.3; the R1 batch (context controls) waits for its own real session
#: acceptance (tasks 3.6 / 4.5). Opening a batch is exactly this edit plus the
#: matching one in ``auth/capability_matrix.py``; everything below fails until
#: the two agree.
ACCEPTED = frozenset({
    "scheduler.instances",
    "scheduler.recipients",
    "scheduler.create",
    "scheduler.runs.list",
    "scheduler.runs.detail",
    "scheduler.runs.delete",
})

#: Registered, implemented, deliberately not served yet.
UNACCEPTED = frozenset({
    "session_context.usage",
    "session_context.compact",
})


class ProjectionDeclarationTest(unittest.TestCase):
    def test_projection_keys_are_exactly_the_eight(self):
        self.assertEqual(
            set(capability_matrix.feature_action_availability()),
            {key for key, _, _ in capability_matrix.FEATURE_ACTIONS})

    def test_the_batch_state_covers_every_action_exactly_once(self):
        keys = {key for key, _, _ in capability_matrix.FEATURE_ACTIONS}
        self.assertEqual(ACCEPTED | UNACCEPTED, keys)
        self.assertEqual(ACCEPTED & UNACCEPTED, set())

    def test_every_action_maps_to_its_own_slice(self):
        """One action per slice is what makes per-action acceptance possible."""
        seen = {}
        for _, slice_id, action in capability_matrix.FEATURE_ACTIONS:
            self.assertNotIn(slice_id, seen,
                             "%s is reused by two feature actions" % slice_id)
            seen[slice_id] = action
            spec = capability_matrix.slice_for(slice_id)
            # An accepted batch declares the one action this key projects -- never
            # a sibling action it was not accepted for.
            self.assertLessEqual(set(spec.declared_open), {action}, slice_id)

    def test_slices_declare_the_batch_state(self):
        for key, slice_id, action in capability_matrix.FEATURE_ACTIONS:
            spec = capability_matrix.slice_for(slice_id)
            self.assertTrue(spec.implemented, slice_id)
            if key in ACCEPTED:
                self.assertTrue(spec.accepted, '%s: %s is accepted but the '
                                "slice is not" % (slice_id, key))
                self.assertEqual(set(spec.open), {action}, slice_id)
            else:
                self.assertFalse(spec.accepted, slice_id)
                self.assertEqual(
                    spec.open, {},
                    "%s is served without its batch's acceptance" % slice_id)

    def test_projection_matches_the_batch_state(self):
        projection = capability_matrix.feature_action_availability()
        for key, _, _ in capability_matrix.FEATURE_ACTIONS:
            expected = ({"available": True, "reason": ""} if key in ACCEPTED
                        else {"available": False, "reason": "not_accepted"})
            self.assertEqual(projection[key], expected, key)

    def test_reason_precedence_puts_not_implemented_first(self):
        spec = capability_matrix.slice_for("session_context_usage")
        spec.implemented = False
        try:
            self.assertEqual(
                capability_matrix.feature_action_availability()[
                    "session_context.usage"]["reason"],
                "not_implemented")
        finally:
            spec.implemented = True

    def test_routes_match_the_batch_state(self):
        for key, slice_id, action in capability_matrix.FEATURE_ACTIONS:
            entry = capability_matrix.route(slice_id, action)
            if key in ACCEPTED:
                self.assertNotEqual(
                    entry["policy"], "closed",
                    "%s.%s is accepted, so the gate must not refuse it"
                    % (slice_id, action))
            else:
                self.assertEqual(entry["policy"], "closed",
                                 "%s.%s" % (slice_id, action))

    def test_original_scheduler_actions_stay_open(self):
        spec = capability_matrix.slice_for("scheduler")
        for action in ("list", "toggle", "update", "delete", "run"):
            self.assertIn(action, spec.open, action)
            self.assertNotEqual(
                capability_matrix.route("scheduler", action)["policy"], "closed")
        # A closed sibling slice must not report the shared consumer as down.
        self.assertTrue(
            capability_matrix.consumer_availability()["scheduler"]["available"])

    def test_matrix_is_self_consistent(self):
        self.assertEqual(capability_matrix.check_consistency(), [])


class DisabledActionsTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            spec.id: (spec.implemented, spec.accepted,
                      dict(spec.open), dict(spec.declared_open))
            for spec in capability_matrix.SLICES
        }

    def tearDown(self):
        capability_matrix.finalize()
        for spec in capability_matrix.SLICES:
            implemented, accepted, open_map, declared = self._saved[spec.id]
            spec.implemented = implemented
            spec.accepted = accepted
            spec.open = dict(open_map)
            spec.declared_open = dict(declared)

    def _open(self, slice_id, action, access):
        """Simulate a declared-and-accepted action.

        Production reaches this state by editing the declaration once the
        batch has real acceptance, so both maps move together; setting only
        ``open`` would be reverted by the next ``finalize()``.
        """
        spec = capability_matrix.slice_for(slice_id)
        spec.accepted = True
        spec.declared_open = {action: access}
        spec.open = {action: access}
        return spec

    def test_unknown_key_is_a_configuration_error(self):
        with self.assertRaises(capability_matrix.CapabilityConfigurationError):
            capability_matrix.parse_disabled_actions("nope.not_a_feature")

    def test_blank_input_is_empty(self):
        self.assertEqual(capability_matrix.parse_disabled_actions(None), frozenset())
        self.assertEqual(capability_matrix.parse_disabled_actions(""), frozenset())
        self.assertEqual(capability_matrix.parse_disabled_actions(" , ,"),
                         frozenset())

    def test_disable_can_only_narrow(self):
        """Disabling an action never rewrites implemented/accepted."""
        spec = self._open("session_context_usage", "usage",
                          capability_matrix.ACCESS_READ)
        capability_matrix.finalize(
            capability_matrix.parse_disabled_actions("session_context.usage"))
        self.assertEqual(spec.open, {})
        self.assertTrue(spec.implemented)
        self.assertTrue(spec.accepted)
        self.assertEqual(
            capability_matrix.feature_action_availability()[
                "session_context.usage"],
            {"available": False, "reason": "disabled_by_deployment"})
        self.assertEqual(
            capability_matrix.route("session_context_usage", "usage")["policy"],
            "closed")

    def test_disabling_one_action_leaves_siblings_untouched(self):
        self._open("session_context_usage", "usage",
                   capability_matrix.ACCESS_READ)
        self._open("session_context_compact", "compact",
                   capability_matrix.ACCESS_EXECUTE)
        capability_matrix.finalize(
            capability_matrix.parse_disabled_actions("session_context.usage"))
        projection = capability_matrix.feature_action_availability()
        self.assertFalse(projection["session_context.usage"]["available"])
        self.assertTrue(projection["session_context.compact"]["available"])

    def test_finalize_is_idempotent(self):
        spec = self._open("scheduler_create", "create",
                          capability_matrix.ACCESS_CONFIG)
        disabled = capability_matrix.parse_disabled_actions("scheduler.create")
        capability_matrix.finalize(disabled)
        capability_matrix.finalize(disabled)
        self.assertEqual(spec.open, {})
        capability_matrix.finalize()
        self.assertEqual(spec.open, {"create": capability_matrix.ACCESS_CONFIG})

    def test_open_slice_route_carries_the_declared_policy(self):
        self._open("scheduler_runs_list", "list",
                   capability_matrix.ACCESS_READ)
        entry = capability_matrix.route("scheduler_runs_list", "list")
        self.assertEqual(entry["policy"], capability_matrix.DEFAULT_POLICY)


def test_auth_context_exposes_feature_actions(web_app):
    """The real endpoint carries all eight keys, in their batch state.

    An old client simply ignores the field; a new client treats a *missing*
    field as "everything closed", so the field's presence is part of the
    contract. What each key says is the declaration's answer, so the two
    batches must read differently here.
    """
    app = web_app("feature-actions")
    app.add_agent("shared-agent")
    token = app.login("root")
    response = app.get("/auth/context", token=token)
    assert response.status.startswith("200"), response.status
    body = json.loads(response.data)
    assert body["status"] == "success"
    actions = body["feature_actions"]
    assert set(actions) == {key for key, _, _ in capability_matrix.FEATURE_ACTIONS}
    for key, entry in actions.items():
        if key in ACCEPTED:
            assert entry == {"available": True, "reason": ""}, (key, entry)
        else:
            assert entry["available"] is False, key
            assert entry["reason"] == "not_accepted", key
    # The pre-existing projections are unchanged by the addition.
    assert body["consumers"]["scheduler"]["available"] is True


if __name__ == "__main__":
    unittest.main()
