# encoding:utf-8
"""Background process handles: the routing facts, without a device (task 6.6).

``tests/test_desktop_remote_dispatch.py`` drives the *whole* delegation, which is
what proves the feature works. This file is the narrower half: the handle service
on its own, against a real store, so the rules that are easy to get subtly wrong
are pinned by themselves --

* which master call *opens* a handle (and which only looks like it could);
* that a handle is idempotent per tool call and refreshes on use;
* that ``close_for_scope`` retires exactly the handles a lost authorization
  covered, and leaves every other account's alone;
* that ``expire_overdue`` retires a handle past its TTL rather than routing to a
  process the device may no longer have.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tests._helpers import WebAppHarness


class _Ctx:
    """The slice of ``AccessContext`` the handle service reads."""

    def __init__(self, user, tenant_id, **ids):
        self.user = user
        self.tenant_id = tenant_id
        self.binding = {"id": ids.get("binding_id", "bw-1")}
        self.workspace = {"id": ids.get("workspace_id", "ws-1")}
        self.device = {"id": ids.get("device_id", "dev-1")}
        self.grant_version = ids.get("grant_version", 1)


def _command(**overrides):
    command = {
        "id": "cmd-1", "tool_name": "bash", "run_id": "run-1",
        "tool_call_id": "tc-1", "tenant_id": "t-1", "agent_id": "agent-1",
        "session_id": "sess-1", "binding_id": "bw-1", "workspace_id": "ws-1",
        "device_id": "dev-1", "grant_version": 1,
    }
    command.update(overrides)
    return command


class HandleServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-handles-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.user = cls.app.member("handle-user", [])
        cls.other = cls.app.member("handle-other", [])

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        from integrations.desktop.process_handles import service_for

        self.service = service_for(self.app.service)
        self.app.service._store.execute(
            "DELETE FROM desktop_process_handles WHERE user_id IN (?, ?)",
            (self.user, self.other))
        # The table's one foreign key is the device: a handle is meaningless
        # without the machine that owns the process, so the rows here are real.
        self.app.service._store.execute("DELETE FROM desktop_devices WHERE id=?",
                                        ("dev-1",))
        self.app.service._store.execute(
            "INSERT INTO desktop_devices (id, user_id, installation_id,"
            " display_name, platform, client_version) VALUES (?,?,?,?,?,?)",
            ("dev-1", self.user, "install-handles-1", "Box", "macos", "2.1.9"))

    # -- the master tool's three shapes --------------------------------------

    def test_only_the_master_background_flag_opens_a_handle(self):
        from integrations.desktop.process_handles import is_background_start

        self.assertTrue(is_background_start(
            "bash", {"command": "python3 s.py", "run_in_background": True}))
        self.assertFalse(is_background_start("bash", {"command": "ls"}))
        # A follow-up is not a start, even if it re-sends the flag: the master
        # tool takes ``bash_id`` as the whole request.
        self.assertFalse(is_background_start(
            "bash", {"bash_id": "job_1", "run_in_background": True}))
        # Only the script tool has a background mode, and only its own argument
        # counts -- a model cannot open a handle by inventing a key.
        self.assertFalse(is_background_start(
            "read", {"path": "a", "run_in_background": True}))
        self.assertFalse(is_background_start(
            "bash", {"command": "ls", "background": True}))

    def test_the_reference_and_the_kill_flag_are_the_masters_own_arguments(self):
        from integrations.desktop.process_handles import (
            background_reference, is_kill,
        )

        self.assertEqual(background_reference({"bash_id": "job_1"}), "job_1")
        self.assertEqual(background_reference({"command": "ls"}), "")
        self.assertEqual(background_reference({"bash_id": "  "}), "")
        self.assertTrue(is_kill({"bash_id": "job_1", "kill": True}))
        self.assertFalse(is_kill({"bash_id": "job_1"}))
        self.assertFalse(is_kill(None))

    # -- recording -----------------------------------------------------------

    def test_a_handle_comes_from_the_devices_payload_and_nowhere_else(self):
        ctx = _Ctx({"id": self.user, "username": "handle-user"}, "t-1")
        self.assertIsNone(self.service.record(
            ctx=ctx, command=_command(), payload={"output": "no id here"}))
        self.assertIsNone(self.service.record(
            ctx=ctx, command=_command(), payload=None))
        self.assertIsNone(self.service.record(
            ctx=ctx, command=_command(), payload={"bash_id": "   "}))
        row = self.service.record(
            ctx=ctx, command=_command(),
            payload={"status": "success", "result": {"bash_id": "job_dev_1"}})
        self.assertEqual(row["id"], "job_dev_1")
        self.assertEqual(row["device_id"], "dev-1")
        self.assertEqual(row["workspace_id"], "ws-1")
        self.assertEqual(row["grant_version"], 1)
        self.assertEqual(row["state"], "live")

    def test_a_redelivered_start_refreshes_one_handle(self):
        ctx = _Ctx({"id": self.user, "username": "handle-user"}, "t-1")
        payload = {"result": {"bash_id": "job_dev_2"}}
        first = self.service.record(ctx=ctx, command=_command(), payload=payload)
        self.app.service._store.execute(
            "UPDATE desktop_process_handles SET expires_at=?, state='expired'"
            " WHERE id=?", (1, first["id"]))
        again = self.service.record(ctx=ctx, command=_command(), payload=payload)
        self.assertEqual(again["id"], first["id"],
                         "one tool call started one process, so one handle")
        self.assertEqual(again["state"], "live")
        self.assertTrue(int(again["expires_at"]) > time.time())
        count = self.app.service._store.execute(
            "SELECT COUNT(*) AS c FROM desktop_process_handles WHERE run_id=?",
            ("run-1",))[0]["c"]
        self.assertEqual(count, 1)

    # -- expiry and scope ----------------------------------------------------

    def test_an_overdue_handle_is_retired_by_ttl(self):
        ctx = _Ctx({"id": self.user, "username": "handle-user"}, "t-1")
        row = self.service.record(
            ctx=ctx, command=_command(),
            payload={"result": {"bash_id": "job_dev_3"}})
        self.app.service._store.execute(
            "UPDATE desktop_process_handles SET expires_at=? WHERE id=?",
            (1, row["id"]))
        self.assertGreaterEqual(self.service.expire_overdue(), 1)
        self.assertEqual(self.service.get(row["id"])["state"], "expired")

    def test_closing_a_scope_retires_only_that_scopes_handles(self):
        ctx = _Ctx({"id": self.user, "username": "handle-user"}, "t-1")
        other_ctx = _Ctx({"id": self.other, "username": "handle-other"}, "t-1")
        mine = self.service.record(
            ctx=ctx, command=_command(),
            payload={"result": {"bash_id": "job_dev_4"}})
        theirs = self.service.record(
            ctx=other_ctx, command=_command(id="cmd-2", run_id="run-2",
                                            tool_call_id="tc-2"),
            payload={"result": {"bash_id": "job_dev_5"}})
        other_project = self.service.record(
            ctx=ctx, command=_command(id="cmd-3", run_id="run-3",
                                      tool_call_id="tc-3", workspace_id="ws-9"),
            payload={"result": {"bash_id": "job_dev_6"}})
        retires = self.service.close_for_scope(user_id=self.user,
                                              workspace_id="ws-1")
        self.assertEqual(retires, 1)
        self.assertEqual(self.service.get(mine["id"])["state"], "expired")
        self.assertEqual(self.service.get(other_project["id"])["state"], "live",
                         "another project's grant was not the one revoked")
        self.assertEqual(self.service.get(theirs["id"])["state"], "live",
                         "another account's handle is untouched")
        # A whole-user revoke (logout / account disable) takes the rest.
        self.assertEqual(self.service.close_for_scope(user_id=self.user), 1)
        self.assertEqual(self.service.get(other_project["id"])["state"],
                         "expired")

    def test_a_stopped_run_retires_its_own_handles(self):
        ctx = _Ctx({"id": self.user, "username": "handle-user"}, "t-1")
        mine = self.service.record(
            ctx=ctx, command=_command(id="cmd-4", run_id="run-4",
                                      tool_call_id="tc-4"),
            payload={"result": {"bash_id": "job_dev_7"}})
        other_run = self.service.record(
            ctx=ctx, command=_command(id="cmd-5", run_id="run-5",
                                      tool_call_id="tc-5"),
            payload={"result": {"bash_id": "job_dev_8"}})
        self.assertEqual(self.service.terminate_for_run("run-4"), 1)
        stopped = self.service.get(mine["id"])
        self.assertEqual(stopped["state"], "terminated")
        self.assertTrue(stopped["terminate_requested_at"])
        self.assertEqual(self.service.get(other_run["id"])["state"], "live")

    def test_a_resolve_refreshes_a_live_handle_and_records_a_kill(self):
        ctx = _Ctx({"id": self.user, "username": "handle-user"}, "t-1")
        row = self.service.record(
            ctx=ctx, command=_command(),
            payload={"result": {"bash_id": "job_dev_9"}})
        # Still inside its TTL, but nearly out: using it must extend it, or a
        # long-running job would fall out of reach while its owner is polling it.
        self.app.service._store.execute(
            "UPDATE desktop_process_handles SET last_seen_at=?, expires_at=?"
            " WHERE id=?", (1, int(time.time()) + 30, row["id"]))

        class _Identity:
            agent_id = "agent-1"
            session_id = "sess-1"

        resolved = self.service.resolve(ref=row["id"], ctx=ctx,
                                       identity=_Identity(),
                                       arguments={"bash_id": row["id"]})
        self.assertEqual(resolved["id"], row["id"])
        self.assertGreater(int(resolved["expires_at"]), int(time.time()) + 60,
                           "using a handle keeps it alive")
        self.assertGreater(int(resolved["last_seen_at"]), 1)
        self.service.resolve(ref=row["id"], ctx=ctx, identity=_Identity(),
                             arguments={"bash_id": row["id"], "kill": True})
        self.assertTrue(self.service.get(row["id"])["terminate_requested_at"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
