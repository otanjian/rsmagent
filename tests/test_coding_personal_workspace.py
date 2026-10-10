# encoding:utf-8
"""The personal coding workspace: one account directory under project root P.

change ``add-coding-agent-personal-workspace``. A coding Agent's configured
project root ``P`` is where its trusted configuration and tools live; personal
business files belong in ``U = P/user/<account>``. This module pins the three
properties that make that safe:

    10|* the account name comes from the identity database (the immutable
  ``user_id``), never from the request, and an unsafe name is refused rather
  than silently rewritten into a path segment;
* the binding is durable and unique, so two windows opening at once land on one
  directory and a retry after a lost response adopts the same one;
* a preparation failure leaves a retryable reservation and never falls back to
  the project root or the platform's own filesystem.

The remote directory preparation is exercised with a scripted transport, so the
    20|whole path is covered without an OpenCode host.
"""

import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.coding import (
    CODING_WORKSPACE_CONFLICT,
    CODING_WORKSPACE_UNSAFE,
    CodingError,
    CodingSettings,
)
from agent.coding.workspace import (
    CODING_USER_WORKSPACES,
    CodingWorkspaceService,
    personal_root,
    safe_username_segment,
    workspace_id_for,
)
from auth.service import IdentityService

PROJECT = "/srv/checkouts/sap-assistant"


def _client(*, fail=False, calls=None):
    """A scripted remote client: it only has to acknowledge a preparation."""

    class _Client:
        def prepare_workspace(self, base_project_dir, effective_directory):
            if calls is not None:
                calls.append((base_project_dir, effective_directory))
            if fail:
                raise CodingError("coding_upstream_unavailable", "disk full", 502)
            return {"effective_directory": effective_directory}

    return _Client()


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="ident-")
        self.svc = IdentityService(f"{self.db}/identity.db")
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=tempfile.mkdtemp(prefix="shared-"), allow_weak=True)
        self.tenant = self.svc.list_tenants()[0]["id"]
        self._admin = self.svc.list_platform_users()[0]["id"]
        self.user_id = self._member("tanjian")
        self.settings = CodingSettings(
            enabled=True, service_id="default",
            api_url="http://oc:4096", web_url="http://oc:3000",
            personal_workspace_enabled=True)

    def _member(self, username):
        return self.svc.create_member(
            actor_user_id=self._admin, tenant_id=self.tenant,
            operation="create-new", username=username,
            display_name=username.title(), temporary_password="TmpPass123!",
            roles=[])["user_id"]

    def service(self, **kwargs):
        return CodingWorkspaceService(
            self.svc, settings=kwargs.pop("settings", self.settings),
            client=kwargs.pop("client", _client()), **kwargs)


# --- the pure path rules --------------------------------------------------


class PathRules(unittest.TestCase):
    def test_a_safe_name_is_one_plain_segment(self):
        self.assertEqual(safe_username_segment("tanjian"), "tanjian")
        self.assertEqual(safe_username_segment("tan_jian-01.x"), "tan_jian-01.x")

    def test_the_canonical_spelling_is_kept_not_folded(self):
        """Logging in a different case must not create a second directory."""
        self.assertEqual(safe_username_segment("Tanjian"), "Tanjian")

    def test_a_name_that_cannot_be_one_segment_is_refused(self):
        for bad in ("", ".", "..", "a b", "a/b", "a\\b", "../x", "a:b", "用户"):
            with self.subTest(bad=bad):
                self.assertIsNone(safe_username_segment(bad))

    def test_the_root_is_project_slash_user_slash_account(self):
        self.assertEqual(personal_root(PROJECT, "tanjian"),
                         f"{PROJECT}/user/tanjian")
        self.assertEqual(personal_root(f"{PROJECT}/", "tanjian"),
                         f"{PROJECT}/user/tanjian")

    def test_the_workspace_id_depends_on_verified_identity_only(self):
        base = dict(service_id="default", tenant_id="t-1", base_project_dir=PROJECT,
                    user_id="u-1")
        wid = workspace_id_for(**base)
        self.assertTrue(wid)
        # Same identity, same id (a retry).
        self.assertEqual(wid, workspace_id_for(**base))
        # Any one field changing gives a different id.
        for field, value in (("service_id", "other"), ("tenant_id", "t-2"),
                             ("base_project_dir", "/srv/other"), ("user_id", "u-2")):
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = value
                self.assertNotEqual(wid, workspace_id_for(**changed))


# --- the binding table ----------------------------------------------------


class BindingSchema(_Fixture):
    def _columns(self, table=CODING_USER_WORKSPACES):
        con = sqlite3.connect(self.svc._store.db_path)
        try:
            return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
        finally:
            con.close()

    def test_the_binding_is_one_protected_table_in_the_identity_database(self):
        columns = self._columns()
        for expected in ("workspace_id", "service_id", "tenant_id", "user_id",
                         "username_at_creation", "base_project_dir",
                         "effective_directory", "state", "created_at"):
            self.assertIn(expected, columns)

    def test_the_effective_directory_can_only_belong_to_one_subject(self):
        con = sqlite3.connect(self.svc._store.db_path)
        try:
            info = list(con.execute(f"PRAGMA index_list({CODING_USER_WORKSPACES})"))
            unique_columns = []
            for row in info:
                if not row[2]:
                    continue
                unique_columns.append(
                    tuple(c[2] for c in
                          con.execute(f"PRAGMA index_info('{row[1]}')")))
        finally:
            con.close()
        self.assertIn(("service_id", "effective_directory"), unique_columns)
        self.assertIn(("service_id", "tenant_id", "base_project_dir", "user_id"),
                      unique_columns)


# --- reserve / reuse / refuse --------------------------------------------


class Preparation(_Fixture):
    def test_first_use_prepares_the_account_directory_and_becomes_ready(self):
        calls = []
        svc = self.service(client=_client(calls=calls))
        ctx = svc.open(agent_id="coder", base_project_dir=PROJECT,
                       user_id=self.user_id, tenant_id=self.tenant)
        self.assertEqual(ctx["effective_directory"], f"{PROJECT}/user/tanjian")
        self.assertEqual(ctx["username"], "tanjian")
        self.assertEqual(ctx["state"], "ready")
        self.assertEqual(ctx["source"], "coding_remote")
        self.assertEqual(ctx["base_project_dir"], PROJECT)
        self.assertEqual(calls, [(PROJECT, f"{PROJECT}/user/tanjian")])

    def test_a_second_open_reuses_the_same_binding_without_a_new_directory(self):
        svc = self.service()
        first = svc.open(agent_id="coder", base_project_dir=PROJECT,
                         user_id=self.user_id, tenant_id=self.tenant)
        second = svc.open(agent_id="coder", base_project_dir=PROJECT,
                          user_id=self.user_id, tenant_id=self.tenant)
        self.assertEqual(first["workspace_id"], second["workspace_id"])
        self.assertEqual(first["effective_directory"], second["effective_directory"])
        rows = self.svc._store.execute(
            f"SELECT COUNT(*) AS c FROM {CODING_USER_WORKSPACES}")
        self.assertEqual(rows[0]["c"], 1)

    def test_another_user_gets_a_different_directory(self):
        other_id = self._member("other")
        svc = self.service()
        mine = svc.open(agent_id="coder", base_project_dir=PROJECT,
                        user_id=self.user_id, tenant_id=self.tenant)
        theirs = svc.open(agent_id="coder", base_project_dir=PROJECT,
                          user_id=other_id, tenant_id=self.tenant)
        self.assertNotEqual(mine["effective_directory"], theirs["effective_directory"])
        self.assertTrue(theirs["effective_directory"].endswith("/user/other"))

    def test_an_unknown_user_is_refused_and_no_directory_is_prepared(self):
        calls = []
        svc = self.service(client=_client(calls=calls))
        with self.assertRaises(CodingError) as raised:
            svc.open(agent_id="coder", base_project_dir=PROJECT,
                     user_id="ghost", tenant_id=self.tenant)
        self.assertEqual(raised.exception.code, CODING_WORKSPACE_UNSAFE)
        self.assertEqual(calls, [])

    def test_an_unsafe_account_name_is_refused_not_rewritten(self):
        weird_id = self._member("ok.name")
        # Corrupt the stored name into something that is not one safe segment.
        con = sqlite3.connect(self.svc._store.db_path)
        try:
            con.execute("UPDATE users SET username='../evil' WHERE id=?",
                        (weird_id,))
            con.commit()
        finally:
            con.close()
        svc = self.service()
        with self.assertRaises(CodingError) as raised:
            svc.open(agent_id="coder", base_project_dir=PROJECT,
                     user_id=weird_id, tenant_id=self.tenant)
        self.assertEqual(raised.exception.code, CODING_WORKSPACE_UNSAFE)

    def test_a_preparation_failure_is_retryable_and_does_not_fall_back(self):
        svc = self.service(client=_client(fail=True))
        with self.assertRaises(CodingError):
            svc.open(agent_id="coder", base_project_dir=PROJECT,
                     user_id=self.user_id, tenant_id=self.tenant)
        rows = self.svc._store.execute(
            f"SELECT state FROM {CODING_USER_WORKSPACES}")
        self.assertEqual([r["state"] for r in rows], ["creating"])
        # A retry with a working remote completes the same reservation.
        svc2 = self.service()
        ctx = svc2.open(agent_id="coder", base_project_dir=PROJECT,
                        user_id=self.user_id, tenant_id=self.tenant)
        self.assertEqual(ctx["state"], "ready")

    def test_a_second_subject_cannot_claim_an_occupied_directory(self):
        # An existing row already owns the canonical directory under a
        # *different* subject (an account alias). It must not be silently
        # shared, and nothing new may be created on top of it.
        con = sqlite3.connect(self.svc._store.db_path)
        try:
            con.execute(
                f"INSERT INTO {CODING_USER_WORKSPACES}"
                " (workspace_id, service_id, tenant_id, user_id,"
                "  username_at_creation, base_project_dir, effective_directory)"
                " VALUES ('oc-ws-alias','default',?,'u-alias','tanjian',?,?)",
                (self.tenant, PROJECT, f"{PROJECT}/user/tanjian"))
            con.commit()
        finally:
            con.close()
        svc = self.service()
        with self.assertRaises(CodingError) as raised:
            svc.open(agent_id="coder", base_project_dir=PROJECT,
                     user_id=self.user_id, tenant_id=self.tenant)
        self.assertEqual(raised.exception.code, CODING_WORKSPACE_CONFLICT)
        # The alias row is untouched and no second row was written.
        rows = self.svc._store.execute(
            f"SELECT workspace_id FROM {CODING_USER_WORKSPACES}")
        self.assertEqual([r["workspace_id"] for r in rows], ["oc-ws-alias"])


# --- creating a personal session ------------------------------------------


class _SessionClient:
    """Stands in for the session half of OpenCode."""

    def __init__(self):
        self.calls = []

    def create_session(self, session_id, project_dir, permission=None):
        from agent.coding.opencode import RemoteSession

        self.calls.append({"op": "create", "dir": project_dir})
        return RemoteSession(id=session_id, title="New session",
                             directory=project_dir, created_ms=1, updated_ms=1)

    def set_permission(self, session_id, project_dir, permission):
        self.calls.append({"op": "set_permission", "dir": project_dir})


class PersonalSessionReserve(_Fixture):
    def _session_service(self, workspace):
        from pathlib import Path

        from agent.coding.sessions import CodingSessionService
        from agent.memory.conversation_store import ConversationStore

        store = ConversationStore(Path(self.db) / "conversations.db")
        client = _SessionClient()
        service = CodingSessionService(
            store, settings=self.settings, client=client, workspace=workspace)
        return service, client, store

    def test_a_personal_reservation_runs_in_the_account_directory(self):
        from common.runtime_identity import identity_scope

        workspace = self.service()
        service, client, store = self._session_service(workspace)
        with identity_scope(agent_id="coder", user_id=self.user_id,
                            tenant_id=self.tenant):
            result = service.reserve(agent_id="coder", project_dir=PROJECT,
                                     request_id="req-1")
        expected = f"{PROJECT}/user/tanjian"
        assert result["project_dir"] == expected
        assert client.calls[0] == {"op": "create", "dir": expected}
        link = store.get_coding_link(result["session_id"])
        assert link["project_dir"] == expected
        assert link["workspace_id"]
        assert result["workspace_id"] == link["workspace_id"]

    def test_a_retry_reuses_the_same_personal_binding(self):
        from common.runtime_identity import identity_scope

        workspace = self.service()
        service, _client, store = self._session_service(workspace)
        with identity_scope(agent_id="coder", user_id=self.user_id,
                            tenant_id=self.tenant):
            first = service.reserve(agent_id="coder", project_dir=PROJECT,
                                    request_id="req-1")
            second = service.reserve(agent_id="coder", project_dir=PROJECT,
                                     request_id="req-1")
        assert first["session_id"] == second["session_id"]
        assert first["workspace_id"] == second["workspace_id"]
        rows = self.svc._store.execute(
            f"SELECT COUNT(*) AS c FROM {CODING_USER_WORKSPACES}")
        assert rows[0]["c"] == 1

    def test_with_the_switch_off_a_session_keeps_the_project_root(self):
        from common.runtime_identity import identity_scope

        plain = CodingSettings(
            enabled=True, service_id="default",
            api_url="http://oc:4096", web_url="http://oc:3000",
            personal_workspace_enabled=False)
        workspace = self.service(settings=plain)
        service, client, store = self._session_service(workspace)
        service.settings = plain
        with identity_scope(agent_id="coder", user_id=self.user_id,
                            tenant_id=self.tenant):
            result = service.reserve(agent_id="coder", project_dir=PROJECT,
                                     request_id="req-1")
        assert result["project_dir"] == PROJECT
        assert client.calls[0]["dir"] == PROJECT
        assert store.get_coding_link(result["session_id"])["workspace_id"] == ""


if __name__ == "__main__":
    unittest.main()
