# encoding:utf-8
"""The v3 ``operations`` authorization (change add-windows-local-project-access, task 3.3).

v2 decided "may this command run" from the tool allow-list alone, so a project
opened for *editing* and a project opened for *running code in* were one
permission. v3 splits that into named operations and this file locks the split:

* the **grant** is the authority, and a command cannot exceed it;
* the **command's snapshot** is what the command was created for, so a grant
  widened after the enqueue cannot retroactively authorize a queued command;
* the **frame's declaration** can only narrow further;
* a row that predates the authorization is read as the **empty set**, never as
  "unrestricted" -- the migration backfills nothing, which is the requirement.

The last one is the reason this file exists at all: "the old grant already allowed
files" is exactly the silent upgrade v3 was created to prevent.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from auth import desktop_contracts_v2 as v2  # noqa: E402
from auth import desktop_contracts_v3 as v3  # noqa: E402
from auth import store as identity_store  # noqa: E402
from auth import store as store_mod  # noqa: E402
from integrations.desktop import execution_broker as broker  # noqa: E402
from integrations.desktop import execution_capability  # noqa: E402
from integrations.desktop.errors import DesktopAccessError  # noqa: E402


class _Ctx:
    """The slice of the broker's access context ``_check_operations`` reads."""

    def __init__(self, operations=None):
        self.binding = {}
        if operations is not None:
            self.binding["operations_json"] = operations


def _command(**overrides):
    command = {
        "protocol_major": 3,
        "tool_name": "write",
        "operations_json": json.dumps(["files"]),
    }
    command.update(overrides)
    return command


class _Checker:
    """Call the real methods without constructing the broker's dependencies."""

    _check_operations = broker.BrokerService._check_operations
    _check_runtime = broker.BrokerService._check_runtime
    _check_quota = broker.BrokerService._check_quota
    _contract = broker.BrokerService._contract


class QuotaRecheckTests(unittest.TestCase):
    """``quota``: the meter is re-read on this request, not remembered.

    Task 3.3 names "a failed metering/audit step" among the refusals that need
    coverage. This is the metering half: ``prepare`` and ``start`` both run the
    check, and the point of running it twice is that a quota which was fine at
    enqueue time can be exhausted by the time the call happens -- so the answer
    must come from the current state, never from the enqueue.

    ``_check_quota`` reaches the meter through ``self._svc``, so a two-method stub
    exercises the real decision without standing up an identity database. That is
    the difference from the earlier persistence attempts: this asserts on the
    *decision*, and the decision reads exactly one thing.
    """

    class _Ctx:
        user = {"id": "u1"}
        tenant_id = "t1"

    def _broker(self, available):
        captured = {}

        class _Svc:
            def quota_available(inner, *, user_id, tenant_id, metric, amount):
                captured.update(user_id=user_id, tenant_id=tenant_id,
                                metric=metric, amount=amount)
                return available

        instance = broker.BrokerService.__new__(broker.BrokerService)
        instance._svc = _Svc()
        return instance, captured

    def test_an_exhausted_quota_refuses_with_limit_exceeded(self):
        instance, captured = self._broker(available=False)
        with self.assertRaises(DesktopAccessError) as caught:
            instance._check_quota({"tool_name": "read"}, self._Ctx())
        self.assertEqual(caught.exception.code, "limit_exceeded")

    def test_an_available_quota_reports_within_limit_without_consuming(self):
        instance, captured = self._broker(available=True)
        result = instance._check_quota({"tool_name": "read"}, self._Ctx())
        self.assertEqual(result, {"metric": "tool_calls", "within_limit": True})
        # The meter is *asked*, never spent: consuming here would charge a call
        # that may never start.
        self.assertEqual(captured["metric"], "tool_calls")
        self.assertEqual(captured["amount"], 1)
        self.assertEqual(captured["tenant_id"], "t1")

    def test_the_check_is_asked_about_the_calling_user_and_tenant(self):
        # A quota read for the wrong user would let one member spend another's
        # allowance, so the identity it is asked with is part of the contract.
        instance, captured = self._broker(available=True)
        instance._check_quota({"tool_name": "bash"}, self._Ctx())
        self.assertEqual(captured["user_id"], "u1")
        self.assertEqual(captured["tenant_id"], "t1")


class RuntimeVerifiedTests(_Checker, unittest.TestCase):
    """``runtime_verified``: the language must be one this deployment verified.

    The contract lists ``runtime_verified`` among the broker's checks and answers
    ``runtime_unavailable`` for ``runtime_not_verified``. Two properties matter and
    both are about *not* overreaching:

    * a **file tool** carries no runtime claim, so this check must let it through
      untouched -- a check that refused file tools would close the whole surface
      it exists to guard;
    * a platform with **nothing recorded either way** keeps the answer its
      capability slice already gave. ``accepted_runtimes`` distinguishes "not
      configured" from "configured with an empty list", and collapsing the two
      here would silently close scripts on every deployment that never wrote the
      setting.

    Inherits :class:`_Checker` because ``_check_runtime`` resolves the contract
    through ``self._contract`` -- the same method the broker itself calls.
    """

    check = _Checker._check_runtime

    @staticmethod
    def _command(**overrides):
        command = {"protocol_major": 3, "tool_name": "read",
                   "operations_json": json.dumps(["files"]), "runtime": None}
        command.update(overrides)
        return command

    def test_a_file_tool_needs_no_runtime(self):
        # Nothing raised: the check is scoped to the script surface.
        self.check(self._command(), None, "posix")

    def test_a_file_tool_is_not_refused_by_a_stray_runtime_value(self):
        # ``runtime`` covers the digest, so a value could be present on a file
        # command. It is not a language claim for a file tool, so it must not be
        # judged as one.
        self.check(self._command(runtime=""), None, "posix")

    def test_a_script_tool_with_no_recorded_language_is_refused(self):
        command = self._command(tool_name="run_script",
                                operations_json=json.dumps(["scripts"]),
                                runtime=None)
        with self.assertRaises(DesktopAccessError) as caught:
            self.check(command, None, "posix")
        self.assertEqual(caught.exception.code, "runtime_not_verified")

    def test_a_recorded_language_the_platform_has_not_verified_is_refused(self):
        command = self._command(tool_name="run_script",
                                operations_json=json.dumps(["scripts"]),
                                runtime="python")
        # An operator wrote down "python is not verified here", which closes the
        # surface rather than leaving it at its default.
        with patch.object(execution_capability, "accepted_runtimes",
                          lambda platform, settings=None: []), \
                patch.object(execution_capability, "runtime_recorded",
                             lambda platform, settings=None: True):
            with self.assertRaises(DesktopAccessError) as caught:
                self.check(command, None, "posix")
        self.assertEqual(caught.exception.code, "runtime_not_verified")

    def test_a_recorded_language_the_platform_has_verified_is_accepted(self):
        command = self._command(tool_name="run_script",
                                operations_json=json.dumps(["scripts"]),
                                runtime="python")
        with patch.object(execution_capability, "accepted_runtimes",
                          lambda platform, settings=None: ["python"]), \
                patch.object(execution_capability, "runtime_recorded",
                             lambda platform, settings=None: True):
            self.check(command, None, "posix")

    def test_a_platform_with_nothing_recorded_keeps_its_capability_answer(self):
        # The escape that keeps a deployment which never wrote the setting from
        # being silently closed: ``accepted_runtimes`` is empty *and* the platform
        # was never recorded, so the capability slice's own answer stands.
        command = self._command(tool_name="run_script",
                                operations_json=json.dumps(["scripts"]),
                                runtime="python")
        with patch.object(execution_capability, "accepted_runtimes",
                          lambda platform, settings=None: []), \
                patch.object(execution_capability, "runtime_recorded",
                             lambda platform, settings=None: False):
            self.check(command, None, "posix")

    def test_a_frame_may_not_substitute_a_language_the_command_was_not_given(self):
        # The digest covers ``runtime``, so a differing declaration also means the
        # device is about to run something the stored digest does not describe.
        command = self._command(tool_name="run_script",
                                operations_json=json.dumps(["scripts"]),
                                runtime="python")
        with self.assertRaises(DesktopAccessError) as caught:
            self.check(command, "powershell", "posix")
        self.assertEqual(caught.exception.code, "command_conflict")

    def test_a_frame_restating_the_authorized_language_is_not_a_conflict(self):
        command = self._command(tool_name="run_script",
                                operations_json=json.dumps(["scripts"]),
                                runtime="python")
        with patch.object(execution_capability, "accepted_runtimes",
                          lambda platform, settings=None: ["python"]), \
                patch.object(execution_capability, "runtime_recorded",
                             lambda platform, settings=None: True):
            self.check(command, "python", "posix")

    def test_a_v2_row_is_not_asked_about_a_runtime(self):
        """``runtime`` is a v3 field, so a v2 row must pass this check untouched.

        A v2 command has no ``runtime`` column at all, so asking it for one would
        refuse every v2 command -- a compatibility break disguised as a new check.
        The regression that made this explicit: the endpoint suite's real v2 rows
        began failing ``start`` with ``runtime_not_verified`` once this check
        existed, which is what this asserts against.
        """
        command = self._command(protocol_major=2, tool_name="bash",
                                operations_json=None, runtime=None)
        self.check(command, None, "posix")

    def test_the_scope_is_a_v3_row_asked_and_a_v2_row_not(self):
        # The pair, asserted together so the scope cannot drift to one side.
        v3_command = self._command(protocol_major=3, tool_name="run_script",
                                   operations_json=json.dumps(["scripts"]),
                                   runtime=None)
        with self.assertRaises(DesktopAccessError):
            self.check(v3_command, None, "posix")
        v2_command = self._command(protocol_major=2, tool_name="bash",
                                   operations_json=None, runtime=None)
        self.check(v2_command, None, "posix")


class OperationsColumnTests(unittest.TestCase):
    """Reading a stored set: absent and empty are different answers."""

    def test_an_absent_column_is_absent_not_empty(self):
        self.assertIsNone(broker._operations_from_column(None))

    def test_a_written_empty_set_is_empty_not_absent(self):
        self.assertEqual(broker._operations_from_column("[]"), [])
        self.assertEqual(broker._operations_from_column(""), [])
        self.assertEqual(broker._operations_from_column([]), [])

    def test_named_operations_round_trip(self):
        self.assertEqual(broker._operations_from_column('["files"]'), ["files"])
        self.assertEqual(
            broker._operations_from_column(json.dumps(["files", "scripts"])),
            ["files", "scripts"])

    def test_a_duplicate_name_is_collapsed(self):
        self.assertEqual(
            broker._operations_from_column('["files", "files"]'), ["files"])

    def test_unparseable_is_absent_never_empty(self):
        """"I could not read the authorization" must not read as "there was none"."""
        self.assertIsNone(broker._operations_from_column("not json"))
        self.assertIsNone(broker._operations_from_column("{oops"))
        self.assertIsNone(broker._operations_from_column("null"))

    def test_a_malformed_member_invalidates_the_whole_set(self):
        """Dropping just the bad name would silently narrow an authorization."""
        self.assertIsNone(broker._operations_from_column('["files", 5]'))
        self.assertIsNone(broker._operations_from_column('["files", ""]'))

    def test_a_non_list_shape_is_absent(self):
        for value in (7, {"files": True}, 3.5):
            self.assertIsNone(broker._operations_from_column(value), value)


class ContractSelectionTests(unittest.TestCase):
    """The major is a property of the row, so a v3 row is judged by v3."""

    def test_a_v3_row_uses_v3(self):
        self.assertIs(_Checker()._contract(_command()), v3)

    def test_a_v2_row_uses_v2(self):
        self.assertIs(_Checker()._contract(_command(protocol_major=2)), v2)

    def test_a_v1_row_is_refused_by_name(self):
        with self.assertRaises(DesktopAccessError) as caught:
            _Checker()._contract(_command(protocol_major=1))
        self.assertEqual(caught.exception.code, "protocol_incompatible")

    def test_a_future_major_is_refused_rather_than_guessed(self):
        with self.assertRaises(DesktopAccessError) as caught:
            _Checker()._contract(_command(protocol_major=9))
        self.assertEqual(caught.exception.code, "protocol_incompatible")

    def test_an_absent_major_is_treated_as_v1(self):
        command = _command()
        del command["protocol_major"]
        with self.assertRaises(DesktopAccessError):
            _Checker()._contract(command)


class OperationsAuthorizationTests(unittest.TestCase):
    def _check(self, command, binding_operations='["files", "scripts"]',
               declared=None):
        return _Checker()._check_operations(
            _Ctx(binding_operations), command, declared)

    def test_a_v2_row_returns_none_so_nothing_changes(self):
        command = _command(protocol_major=2)
        self.assertIsNone(_Checker()._check_operations(_Ctx('["files"]'), command))

    def test_a_file_command_under_a_file_grant_is_allowed(self):
        self.assertEqual(self._check(_command()), ["files"])

    def test_a_script_command_under_a_file_only_grant_is_refused(self):
        command = _command(tool_name="run_script", operations_json='["scripts"]')
        with self.assertRaises(DesktopAccessError) as caught:
            self._check(command, binding_operations='["files"]')
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_a_script_command_under_a_script_grant_is_allowed(self):
        command = _command(tool_name="run_script", operations_json='["scripts"]')
        self.assertEqual(self._check(command), ["scripts"])

    def test_a_grant_with_no_operations_authorizes_nothing(self):
        for empty in (None, "[]", ""):
            with self.assertRaises(DesktopAccessError) as caught:
                self._check(_command(), binding_operations=empty)
            self.assertEqual(caught.exception.code, "permission_denied", empty)

    def test_a_command_with_no_snapshot_is_refused(self):
        """A v3 row from before the authorization existed gets nothing."""
        for empty in (None, "", "[]"):
            with self.assertRaises(DesktopAccessError) as caught:
                self._check(_command(operations_json=empty))
            self.assertEqual(caught.exception.code, "permission_denied", empty)

    def test_a_command_may_not_exceed_its_grant(self):
        with self.assertRaises(DesktopAccessError) as caught:
            self._check(_command(operations_json='["files", "scripts"]'),
                        binding_operations='["files"]')
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_a_tool_whose_operation_is_missing_is_refused(self):
        """The point of the split: file access alone must not run a script."""
        command = _command(tool_name="bash", operations_json='["files"]')
        with self.assertRaises(DesktopAccessError) as caught:
            self._check(command)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_the_declared_set_may_narrow_but_not_widen(self):
        self.assertEqual(
            self._check(_command(operations_json='["files", "scripts"]'),
                        declared=["files"]),
            ["files", "scripts"])
        with self.assertRaises(DesktopAccessError) as caught:
            self._check(_command(operations_json='["files"]'),
                        declared=["files", "scripts"])
        self.assertEqual(caught.exception.code, "command_conflict")

    def test_an_absent_declaration_is_not_a_widening(self):
        self.assertEqual(self._check(_command(), declared=None), ["files"])

    def test_an_unreadable_snapshot_is_refused_like_an_absent_one(self):
        with self.assertRaises(DesktopAccessError):
            self._check(_command(operations_json="not json"))

    def test_a_grant_widened_after_the_enqueue_does_not_apply(self):
        """The snapshot decides, not the live grant.

        The command was created for files only; a later grant that adds scripts
        must not turn the queued command into a script command.
        """
        command = _command(tool_name="run_script", operations_json='["files"]')
        with self.assertRaises(DesktopAccessError) as caught:
            self._check(command, binding_operations='["files", "scripts"]')
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_every_refusal_names_the_operation_that_was_missing(self):
        command = _command(tool_name="run_script", operations_json='["files"]')
        with self.assertRaises(DesktopAccessError) as caught:
            self._check(command)
        self.assertIn("scripts", str(caught.exception))


class MigrationTests(unittest.TestCase):
    """Migration 50 adds the columns and backfills nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="desktop-ops-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "identity.db")
        self.store = identity_store.IdentityStore(self.path)
        self.store._migrate()

    def _columns(self, table):
        con = sqlite3.connect(self.path)
        try:
            return [row[1] for row in con.execute("PRAGMA table_info(%s)" % table)]
        finally:
            con.close()

    def test_the_operations_columns_exist(self):
        self.assertIn("operations_json",
                      self._columns("desktop_binding_workspaces"))
        self.assertIn("operations_json", self._columns("desktop_commands"))
        self.assertIn("runtime", self._columns("desktop_commands"))

    def test_migration_50_is_registered_and_the_versions_stay_dense(self):
        versions = identity_store.migration_versions()
        self.assertEqual(versions, list(range(1, len(versions) + 1)))
        self.assertIn(50, versions)

    def test_running_the_migrations_again_is_a_no_op(self):
        before = self._columns("desktop_commands")
        self.store._migrate()
        self.assertEqual(self._columns("desktop_commands"), before)

    def test_a_pre_existing_row_reads_as_no_operations(self):
        """The requirement, stated as a test: old rows gain no permission.

        This reproduces the real upgrade path rather than asserting on a fresh
        database: the schema is built *without* migration 50, rows are inserted
        the way the old code inserted them, and only then is migration 50 applied.
        The columns must come out NULL -- which the v3 path reads as the empty set
        -- because nothing in the migration is allowed to backfill a permission
        the user never confirmed.
        """
        path = os.path.join(self._tmp.name, "pre-v3.db")
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            for migration in identity_store._migrations[:49]:
                migration(con)
            con.executescript("""
                INSERT INTO users(id, username, display_name, password_hash)
                    VALUES('u1', 'u1', 'U One', 'x');
                INSERT INTO tenants(id, code, name, shared_root)
                    VALUES('t1', 't1', 'Tenant One', '/tmp/t1');
                INSERT INTO desktop_devices(id, user_id, installation_id,
                    display_name, platform, client_version)
                    VALUES('d1', 'u1', 'inst-1', 'Dev', 'windows', '2.2.0');
                INSERT INTO desktop_workspaces(id, user_id, tenant_id, device_id,
                    label, grant_version)
                    VALUES('w1', 'u1', 't1', 'd1', 'project', 1);
                INSERT INTO desktop_bindings(id, user_id, tenant_id, device_id,
                    agent_id, business_session_id, context_nonce)
                    VALUES('b1', 'u1', 't1', 'd1', 'a1', 's1', 'n1');
                INSERT INTO desktop_binding_workspaces(id, binding_id, workspace_id,
                    grant_version)
                    VALUES('bw1', 'b1', 'w1', 1);
                INSERT INTO desktop_commands(id, request_id, user_id, tenant_id,
                    device_id, binding_id, agent_id, business_session_id, op,
                    params_json, params_sha256, dedupe_key, state, deadline_at,
                    protocol_major, tool_name)
                    VALUES('c1', 'r1', 'u1', 't1', 'd1', 'b1', 'a1', 's1',
                            'read_text', '{}', 'sha', 'dk', 'queued', 0, 3, 'write');
            """)
            con.commit()
            # The pre-migration schema has no operations columns at all.
            self.assertNotIn("operations_json", self._columns_of(con, "desktop_commands"))

            identity_store._migration_50(store_mod._MigrationConnection(con))
            con.commit()

            binding_row = self._row(con, "desktop_binding_workspaces")
            command_row = self._row(con, "desktop_commands")
        finally:
            con.close()

        self.assertIn("operations_json", binding_row)
        self.assertIn("runtime", command_row)
        self.assertIsNone(binding_row["operations_json"])
        self.assertIsNone(command_row["operations_json"])
        self.assertIsNone(command_row["runtime"])
        # ...which the v3 path reads as "no authorization at all".
        self.assertIsNone(
            broker._operations_from_column(binding_row["operations_json"]))
        self.assertIsNone(
            broker._operations_from_column(command_row["operations_json"]))

    @staticmethod
    def _columns_of(con, table):
        return [row[1] for row in con.execute("PRAGMA table_info(%s)" % table)]

    @staticmethod
    def _row(con, table):
        cursor = con.execute("SELECT * FROM %s" % table)
        names = [description[0] for description in cursor.description]
        return dict(zip(names, cursor.fetchone()))


class TheFrameCarriesTheAuthorizationTests(unittest.TestCase):
    """The row's own major decides the frame, and v3's frame names its permission.

    Writing the columns is only half of it: a v3 row emitted as a v2 frame would
    hand the device a frame whose covered field list differs from the one its
    digest was computed over, and the device would reject a frame the server
    believed it had validated. These tests pin the emitting side.
    """

    @staticmethod
    def _row(**overrides):
        row = {
            "id": "dcmd_1",
            "protocol_major": 3,
            "binding_id": "bind_1",
            "device_id": "dev_1",
            "workspace_id": "ws_1",
            "grant_version": 1,
            "selection_generation": 1,
            "connection_epoch": "epoch_1",
            "tool_name": "read",
            "tool_schema_version": v3.TOOL_SCHEMA_VERSION,
            "params": {"relative_path": "a.txt"},
            "params_digest": "sha256:" + "a" * 64,
            "deadline_at": 2_000_000_000,
            "run_id": "run_1",
            "tool_call_id": "call_1",
            "origin": "runtime",
            "operations_json": json.dumps(["files"]),
            "runtime": "",
        }
        row.update(overrides)
        return row

    def test_a_v3_row_is_emitted_as_a_v3_frame_with_its_operations(self):
        from integrations.desktop import execution_payload

        frame = execution_payload.device_execution_frame(self._row())
        self.assertEqual(frame["protocol_major"], 3)
        self.assertEqual(frame["operations"], ["files"])
        # v3's schema version, not v2's: the device checks this field.
        self.assertEqual(frame["tool_schema_version"], v3.TOOL_SCHEMA_VERSION)

    def test_a_v2_row_is_still_emitted_as_a_v2_frame_and_carries_no_operations(self):
        from integrations.desktop import execution_payload

        row = self._row(protocol_major=2, tool_name="bash",
                        tool_schema_version=v2.TOOL_SCHEMA_VERSION,
                        params={"command": "echo hi", "timeout": 30},
                        operations_json=None)
        frame = execution_payload.device_execution_frame(row)
        self.assertEqual(frame["protocol_major"], 2)
        self.assertEqual(frame["tool_schema_version"], v2.TOOL_SCHEMA_VERSION)
        # A v2 frame must not grow a field its contract does not know: "unknown
        # field" on the device side would refuse a frame that used to be valid.
        self.assertNotIn("operations", frame)

    def test_a_v3_row_with_no_recorded_operations_is_refused_rather_than_sent(self):
        from integrations.desktop import execution_payload

        # A row from before migration 50: nothing was ever recorded. The server
        # must refuse to build the frame, not send one the device would refuse.
        row = self._row(operations_json=None)
        with self.assertRaises(ValueError) as caught:
            execution_payload.device_execution_frame(row)
        self.assertIn("operations", str(caught.exception))

    def test_an_old_row_is_never_upgraded_to_v3_by_its_tool_name(self):
        from integrations.desktop import execution_payload

        # ``read`` is a file tool under both majors, so a v2 row could be mistaken
        # for a v3 one if the major were inferred from the tool. Migration
        # backfills nothing, so nothing here may invent an authorization.
        row = self._row(protocol_major=2, operations_json=None,
                        tool_schema_version=v2.TOOL_SCHEMA_VERSION)
        frame = execution_payload.device_execution_frame(row)
        self.assertEqual(frame["protocol_major"], 2)
        self.assertNotIn("operations", frame)

    def test_a_script_frame_carries_the_runtime_it_was_verified_with(self):
        from integrations.desktop import execution_payload

        row = self._row(tool_name="run_script",
                        params={"language": "python", "script": "print(1)"},
                        operations_json=json.dumps(["scripts"]),
                        runtime="python")
        frame = execution_payload.device_execution_frame(row)
        self.assertEqual(frame["operations"], ["scripts"])
        self.assertEqual(frame["runtime"], "python")

    def test_a_frame_whose_runtime_the_platform_cannot_offer_is_refused(self):
        from integrations.desktop import execution_payload

        # ``runtime_verified`` is the check this pins. On win32 the contract names
        # no script launcher, so a script frame must not leave the server -- the
        # refusal happens here rather than on a device that would have to guess
        # what to do with it.
        row = self._row(tool_name="run_script",
                        params={"language": "python", "script": "print(1)"},
                        operations_json=json.dumps(["scripts"]),
                        runtime="python")
        with self.assertRaises(ValueError) as caught:
            execution_payload.device_execution_frame(row, platform="win32")
        self.assertIn("run_script", str(caught.exception))

    def test_a_language_the_contract_does_not_know_is_refused(self):
        from integrations.desktop import execution_payload

        row = self._row(tool_name="run_script",
                        params={"language": "ruby", "script": "puts 1"},
                        operations_json=json.dumps(["scripts"]),
                        runtime="ruby")
        with self.assertRaises(ValueError) as caught:
            execution_payload.device_execution_frame(row, platform="posix")
        self.assertIn("ruby", str(caught.exception))

    def test_a_major_this_build_cannot_serve_is_refused_by_name(self):
        from integrations.desktop import execution_payload

        with self.assertRaises(ValueError) as caught:
            execution_payload.device_execution_frame(self._row(protocol_major=9))
        self.assertIn("9", str(caught.exception))


class AV3CommandMustNameItsAuthorizationTests(unittest.TestCase):
    """The enqueue gate: a v3 row without operations cannot come into existence.

    The contract says a v3 frame "cannot name the authorization it is spending"
    and is refused before any tool runs. Refusing at dispatch would be too late to
    be a guarantee: the row would exist, the user would be waiting on it, and the
    refusal would read as a mysterious dispatch failure. The writer is the right
    place, so the check lives at the top of ``_enqueue_command`` -- before it
    touches the database, which is also why this test needs no harness.
    """

    def _call(self, **overrides):
        from integrations.desktop import commands as commands_mod

        service = commands_mod.CommandService.__new__(commands_mod.CommandService)
        kwargs = dict(
            ctx=None, tenant_id="t1", binding_id="b1", workspace_id="w1",
            grant_version=1, op="execute_tool", params={"relative_path": "a.txt"},
            request_id="req_1", deadline_at=2_000_000_000,
            protocol_major=3, tool_name="read",
            tool_schema_version=v3.TOOL_SCHEMA_VERSION, run_id="run_1",
            tool_call_id="call_1", selection_generation=1,
            operations=None, runtime=None)
        kwargs.update(overrides)
        return service._enqueue_command(**kwargs)

    def test_a_v3_command_without_operations_is_refused_before_the_database(self):
        with self.assertRaises(DesktopAccessError) as caught:
            self._call(operations=None)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIn("operations", str(caught.exception))

    def test_a_v3_command_with_an_empty_operations_list_is_refused(self):
        # ``[]`` is a recorded "no authorization", which authorizes nothing. A v3
        # command may not be enqueued with one.
        with self.assertRaises(DesktopAccessError) as caught:
            self._call(operations=[])
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_a_v2_command_is_not_asked_for_operations(self):
        # v2 had no such concept. Asking it for one would break every existing
        # caller, so the guard is scoped to v3 and this asserts the scope.
        try:
            self._call(protocol_major=2, operations=None)
        except DesktopAccessError as error:
            self.fail("a v2 command must not need operations: %s" % error)
        except Exception:
            # Past the guard, the bare instance has no store to write to. Reaching
            # *any* other failure means the guard let it through, which is the
            # property under test.
            pass


class OperationsPersistenceTests(unittest.TestCase):
    """The writer stores the set, and stores "absent" as absent.

    Nothing wrote these two columns before this change (they existed since
    migration 50 and every reader treated them as NULL), so the reader's careful
    absent/empty/unparseable distinction had nothing to read.

    These assertions are on the **writer itself** -- the printed SQL and the tuple
    bound to it -- rather than on a row produced by a stand-in for the whole
    identity service. Two earlier versions of this test tried the stand-in and
    each failed on the next dependency ``_enqueue_command`` reaches (the
    transaction guard's own ``execute``, then ``_audit``, then ``ctx.user
    ["username"]``): stubbing an entire service to observe one INSERT makes the
    test a description of the stub, not of the writer. Reading the statement keeps
    the assertion on the thing that changed.
    """

    def _columns_and_values(self, operations, runtime, protocol_major=3):
        """Run the writer, returning ``{column: bound value}`` for the INSERT."""
        from integrations.desktop import commands as commands_mod

        captured = {}

        class _Cursor:
            def fetchall(inner):
                return []

            def fetchone(inner):
                return {"c": 0}

        class _Guard:
            def execute(inner, sql, args=()):
                if "INSERT INTO desktop_commands" in sql:
                    columns = sql.split("(", 1)[1].split(")", 1)[0]
                    names = [name.strip() for name in columns.split(",")]
                    captured["row"] = dict(zip(names, args))
                return _Cursor()

            def commit(inner):
                pass

            def __enter__(inner):
                return inner

            def __exit__(inner, *_exc):
                return False

        class _Audit:
            def record(inner, *args, **kwargs):
                return None

        class _Svc:
            _audit = _Audit()

            def _tx(inner):
                return _Guard()

        service = commands_mod.CommandService.__new__(commands_mod.CommandService)
        service._svc = _Svc()

        class _Ctx:
            user = {"id": "u1", "username": "u1"}
            binding = {"device_id": "d1", "agent_id": "a1",
                       "business_session_id": "s1"}

        try:
            service._enqueue_command(
                ctx=_Ctx(), tenant_id="t1", binding_id="b1", workspace_id="w1",
                grant_version=1, op="execute_tool",
                params={"relative_path": "a.txt"}, request_id="req_1",
                deadline_at=2_000_000_000, protocol_major=protocol_major,
                tool_name="read",
                tool_schema_version=v3.TOOL_SCHEMA_VERSION, run_id="run_1",
                tool_call_id="call_1", selection_generation=1,
                operations=operations, runtime=runtime)
        except Exception:
            # Reaching the post-write reads is not what is being tested.
            pass
        return captured.get("row", {})

    def test_the_insert_names_both_v3_columns(self):
        row = self._columns_and_values(operations=["files"], runtime=None)
        self.assertIn("operations_json", row)
        self.assertIn("runtime", row)

    def test_the_column_count_matches_the_bound_values(self):
        # A column added without a value (or the reverse) is a SQL error at
        # runtime, and this is the cheapest place to catch it.
        row = self._columns_and_values(operations=["files"], runtime="python")
        self.assertTrue(row, "the INSERT must have been executed")
        self.assertEqual(len(row), 32)

    def test_the_recorded_set_is_what_the_broker_will_read(self):
        row = self._columns_and_values(operations=["files"], runtime=None)
        self.assertEqual(row["operations_json"], json.dumps(["files"]))
        # ...through the *only* reader, so the writer and the authorizer agree.
        self.assertEqual(
            broker._operations_from_column(row["operations_json"]), ["files"])

    def test_a_script_command_records_the_verified_runtime(self):
        row = self._columns_and_values(operations=["scripts"], runtime="python")
        self.assertEqual(row["runtime"], "python")

    def test_no_operations_is_stored_as_null_never_as_an_empty_list(self):
        # The distinction the reader depends on: NULL is "nothing was ever
        # recorded" and must not be flattened into "the empty set was confirmed".
        row = self._columns_and_values(operations=None, runtime=None,
                                       protocol_major=2)
        self.assertIsNone(row["operations_json"])
        self.assertIsNone(broker._operations_from_column(row["operations_json"]))

    def test_the_stored_json_round_trips_through_the_only_reader(self):
        for operations in (["files"], ["scripts"], ["files", "scripts"]):
            with self.subTest(operations=operations):
                row = self._columns_and_values(operations=operations, runtime=None)
                self.assertEqual(
                    broker._operations_from_column(row["operations_json"]),
                    operations)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
