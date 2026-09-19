# encoding:utf-8
"""Service-level delegation: authorization, the four actions, and auditing.

The store tests already cover the row mechanics. What is pinned here is the
*policy*: who may delegate what to whom, that the delegator loses the ability to
act on the handler's behalf, that every refusal happens before any write, and
that an audit failure stops the delegation from landing.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.todo.service import (
    TodoActor,
    TodoConflictError,
    TodoFieldValidationError,
    TodoNotFoundError,
    TodoPermissionDenied,
    TodoService,
    TodoUnavailable,
)

ME = "user-1"
PEER = "user-2"
THIRD = "user-3"

MEMBERS = {
    "peer": {"user_id": PEER, "username": "peer", "display_name": "Peer"},
    "third": {"user_id": THIRD, "username": "third", "display_name": "Third"},
    # The actor is resolvable too, so "delegate to myself" and "transfer back to
    # the owner" are decided by the delegation rules rather than by a lookup miss.
    "me": {"user_id": ME, "username": "me", "display_name": "Me"},
}


def _service(
    db_dir,
    *,
    owner=ME,
    permissions=("todo.read", "todo.write", "todo.assign"),
    members=MEMBERS,
    audit=None,
    scope="tenant-1",
):
    actor = TodoActor(
        bound=True, scope_id=scope, owner_id=owner, username=owner,
        permissions=set(permissions),
    )
    return TodoService(
        actor,
        enabled_fn=lambda: True,
        db_path=str(Path(db_dir) / "todo" / "todos.db"),
        member_resolver=lambda username: members.get(username),
        audit_recorder=audit,
    )


class _DelegationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.audit = []
        self.svc = _service(self.tmp, audit=self.audit.append)

    def create(self, svc=None, **kw):
        svc = svc or self.svc
        return svc.create(title=kw.pop("title", "t"), **kw)

    def peer_service(self, **kw):
        return _service(self.tmp, owner=PEER, audit=self.audit.append, **kw)


class AssignActionTests(_DelegationCase):
    def test_assign_moves_the_item_to_the_receiver(self):
        item = self.create()

        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        self.assertEqual(moved["assignee_id"], PEER)
        self.assertEqual(moved["owner_id"], ME)
        self.assertEqual(moved["status"], "pending")

        # Gone from my list, and not counted toward my badge any more.
        self.assertEqual(self.svc.list()["total"], 0)
        self.assertEqual(self.svc.summary()["open"], 0)
        # Present in the receiver's list.
        peer = self.peer_service()
        self.assertEqual([i["id"] for i in peer.list()["items"]], [item["id"]])

    def test_delegation_does_not_touch_status_due_or_content(self):
        item = self.create(title="keep me", due_at="2030-01-01T00:00:00+08:00",
                           timezone="Asia/Shanghai", priority="high")

        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        self.assertEqual(moved["title"], "keep me")
        self.assertEqual(moved["due_at"], item["due_at"])
        self.assertEqual(moved["priority"], "high")
        self.assertEqual(moved["status"], item["status"])

    def test_delegator_cannot_act_for_the_receiver(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        # Read access survives (that is what makes recall possible)...
        self.assertEqual(self.svc.get(item["id"])["assignee_id"], PEER)
        # ...but every write is refused, with the same answer as an unknown id.
        for call in (
            lambda: self.svc.update(item["id"], expected_version=moved["version"],
                                    status="completed"),
            lambda: self.svc.update(item["id"], expected_version=moved["version"],
                                    fields={"title": "nope"}),
        ):
            with self.assertRaises(TodoNotFoundError):
                call()

    def test_receiver_can_complete_it(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        peer = self.peer_service()
        done = peer.update(moved["id"], expected_version=moved["version"],
                           status="completed")
        self.assertEqual(done["status"], "completed")

    def test_only_pending_items_can_be_delegated(self):
        item = self.create()
        done = self.svc.update(item["id"], expected_version=item["version"],
                               status="completed")

        with self.assertRaises(TodoConflictError):
            self.svc.assign(item["id"], target_username="peer",
                            expected_version=done["version"])
        self.assertEqual(self.svc.get(item["id"])["assignee_id"], ME)

    def test_stale_version_is_refused(self):
        item = self.create()
        with self.assertRaises(TodoConflictError):
            self.svc.assign(item["id"], target_username="peer", expected_version=99)


class DelegationTargetTests(_DelegationCase):
    def test_unknown_target_is_refused_without_writing(self):
        item = self.create()
        with self.assertRaises(TodoFieldValidationError):
            self.svc.assign(item["id"], target_username="nobody",
                            expected_version=item["version"])
        self.assertEqual(self.svc.get(item["id"])["assignee_id"], ME)

    def test_cannot_delegate_to_myself(self):
        item = self.create()
        with self.assertRaises(TodoFieldValidationError):
            self.svc.assign(item["id"], target_username="me",
                            expected_version=item["version"])
        self.assertEqual(self.svc.get(item["id"])["assignee_id"], ME)

    def test_cross_tenant_target_is_refused_and_audited(self):
        item = self.create()
        # A resolver that finds nothing is exactly what a cross-tenant name looks
        # like: not found, never "found but forbidden".
        with self.assertRaises(TodoFieldValidationError):
            self.svc.assign(item["id"], target_username="outsider",
                            expected_version=item["version"])

        self.assertEqual(self.svc.get(item["id"])["assignee_id"], ME)
        self.assertTrue(
            any(e.get("result") == "denied" for e in self.audit),
            "a refused delegation attempt must leave a denied audit event",
        )

    def test_missing_permission_refuses_before_any_write(self):
        svc = _service(self.tmp, permissions=("todo.read", "todo.write"),
                       audit=self.audit.append)
        item = svc.create(title="t", create_key="k1")

        with self.assertRaises(TodoPermissionDenied):
            svc.assign(item["id"], target_username="peer",
                       expected_version=item["version"])

        self.assertEqual(svc.get(item["id"])["assignee_id"], ME)
        self.assertEqual(self.audit, [])


class TransferRecallRejectTests(_DelegationCase):
    def test_receiver_can_transfer_to_a_third_party(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        peer = self.peer_service()
        handed_on = peer.transfer(moved["id"], target_username="third",
                                  expected_version=moved["version"])

        self.assertEqual(handed_on["assignee_id"], THIRD)
        self.assertEqual(handed_on["owner_id"], ME)
        # The original delegator still reads it and can still recall it.
        self.assertEqual(self.svc.get(item["id"])["assignee_id"], THIRD)

    def test_delegator_can_recall(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        back = self.svc.recall(moved["id"], expected_version=moved["version"])

        self.assertEqual(back["assignee_id"], ME)
        self.assertEqual(back["owner_id"], ME)
        self.assertEqual(self.svc.list()["total"], 1)
        self.assertEqual(self.peer_service().list()["total"], 0)

    def test_receiver_can_reject_back_to_the_owner(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        peer = self.peer_service()
        returned = peer.reject(moved["id"], expected_version=moved["version"])

        self.assertEqual(returned["assignee_id"], ME)
        self.assertEqual(self.svc.list()["total"], 1)

    def test_the_holder_cannot_use_the_wrong_action(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])
        peer = self.peer_service()

        # I no longer hold it, so recall/transfer are mine to give but refuse
        # when somebody else holds it... and the receiver cannot recall.
        with self.assertRaises(TodoConflictError):
            peer.recall(moved["id"], expected_version=moved["version"])
        # The delegator cannot transfer an item somebody else is holding.
        with self.assertRaises(TodoConflictError):
            self.svc.transfer(moved["id"], target_username="third",
                              expected_version=moved["version"])

    def test_multi_hop_chain_is_readable_and_intact(self):
        item = self.create()
        first = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])
        peer = self.peer_service()
        second = peer.transfer(first["id"], target_username="third",
                               expected_version=first["version"])

        events = self.svc.events(second["id"])["items"]
        actions = [e["action"] for e in events]
        self.assertEqual(actions[0], "transfer")
        self.assertEqual(actions[1], "assign")
        self.assertEqual(events[0]["changed"]["to_assignee"], THIRD)
        self.assertEqual(events[1]["changed"]["from_assignee"], ME)
        # Every event is filed under the stable owner, not the current holder.
        for event in events:
            self.assertEqual(event["owner_id"], ME)

    def test_transfer_back_to_the_owner_is_refused_as_reject(self):
        """Handing it back to the delegator is 退回, so 转交 must not do it.

        The target resolves successfully, so this exercises the delegation rule
        rather than a lookup miss.
        """
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])
        peer = self.peer_service()

        with self.assertRaises(TodoFieldValidationError) as caught:
            peer.transfer(moved["id"], target_username="me",
                          expected_version=moved["version"])

        self.assertIn("退回", str(caught.exception))
        self.assertEqual(self.svc.get(item["id"])["assignee_id"], PEER)


class DelegationAuditTests(_DelegationCase):
    def test_success_is_recorded_with_both_ends(self):
        item = self.create()
        self.svc.assign(item["id"], target_username="peer",
                        expected_version=item["version"])

        self.assertEqual(len(self.audit), 1)
        event = self.audit[0]
        self.assertEqual(event["action"], "todo.assign")
        self.assertEqual(event["target"], item["id"])
        self.assertEqual(event["result"], "success")
        self.assertEqual(event["changes"]["from_assignee"], ME)
        self.assertEqual(event["changes"]["to_assignee"], PEER)

    def test_audit_failure_prevents_the_delegation(self):
        def boom(_event):
            raise RuntimeError("audit store down")

        svc = _service(self.tmp, audit=boom)
        item = svc.create(title="t", create_key="k1")

        with self.assertRaises(TodoUnavailable):
            svc.assign(item["id"], target_username="peer",
                       expected_version=item["version"])

        # The item did not move: an action without its audit record must not
        # exist, so the audit is written first.
        self.assertEqual(svc.get(item["id"])["assignee_id"], ME)
        self.assertEqual(svc.get(item["id"])["version"], item["version"])


class DelegationSubjectTests(_DelegationCase):
    """Who acted, recorded on the processing-history event."""

    def test_human_delegation_is_recorded_as_human(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])

        events = self.svc.events(moved["id"])["items"]
        self.assertEqual(events[0]["action"], "assign")
        self.assertEqual(events[0]["operator_kind"], "human")
        self.assertEqual(events[0]["operator_id"], ME)

    def test_agent_delegating_for_the_user_is_recorded_as_agent(self):
        item = self.create()
        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"],
                                operator_id="agent-42")

        events = self.svc.events(moved["id"])["items"]
        self.assertEqual(events[0]["operator_kind"], "agent")
        self.assertEqual(events[0]["operator_id"], "agent-42")
        # The handler still moved: acting through an Agent is the same action,
        # only the recorded subject differs.
        self.assertEqual(moved["assignee_id"], PEER)


class DelegationProjectionTests(_DelegationCase):
    def test_projection_reports_who_may_do_what(self):
        item = self.create()
        mine = self.svc.get(item["id"])
        self.assertTrue(mine["can_delegate"])
        self.assertFalse(mine["can_recall"])

        moved = self.svc.assign(item["id"], target_username="peer",
                                expected_version=item["version"])
        # As the delegator I may read and recall, never act on the content.
        as_delegator = self.svc.get(item["id"])
        self.assertTrue(as_delegator["can_recall"])
        self.assertFalse(as_delegator["can_delegate"])
        self.assertFalse(as_delegator["can_edit"])
        self.assertFalse(as_delegator["can_operate"]["complete"])

        peer = self.peer_service()
        as_handler = peer.get(moved["id"])
        self.assertTrue(as_handler["can_edit"])
        self.assertTrue(as_handler["can_operate"]["complete"])
        self.assertTrue(as_handler["can_delegate"])

    def test_projection_withholds_delegation_without_the_permission(self):
        svc = _service(self.tmp, permissions=("todo.read", "todo.write"))
        item = svc.create(title="t", create_key="k1")
        view = svc.get(item["id"])
        self.assertFalse(view["can_delegate"])
        self.assertFalse(view["can_recall"])

    def test_delegated_view_lists_what_i_handed_out(self):
        mine = self.create(title="mine")
        handed = self.create(title="handed", create_key="k2")
        self.svc.assign(handed["id"], target_username="peer",
                        expected_version=handed["version"])

        view = self.svc.delegated()
        self.assertEqual([i["id"] for i in view["items"]], [handed["id"]])
        self.assertEqual(view["total"], 1)
        # The two views never double count.
        self.assertEqual(self.svc.list()["total"], 1)
        self.assertEqual(self.svc.list()["items"][0]["id"], mine["id"])


if __name__ == "__main__":
    unittest.main()
