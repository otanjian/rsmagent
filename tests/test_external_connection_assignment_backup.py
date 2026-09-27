# encoding:utf-8
"""Assignment state through the backup/restore and schema-migration paths.

Change ``add-external-connection-agent-assignment``, task group 5.3.

The assignment tables hold the *only* record that a connection was narrowed to
named Agents. Losing that record is not an ordinary data loss: the runtime is
required to read a missing state as 缺失应有状态 and refuse, but a state that was
never captured — a backup taken before this change, a database re-migrated from
one — would come back as 沿用原权限 and silently widen every connection a tenant had
narrowed. Four properties keep that from being reachable:

* the two tables are in the backup, so the restriction is part of the state an
  operator restores;
* a tenant's backup holds that tenant's relations — including the ones that
  name a *platform* template as their logical connection;
* restoring the captured rows reproduces the same ``configured``/relation answer,
  and re-opening the database keeps it;
* a backup that cannot speak about assignment is refused by the restore entry
  point while the deployment still relies on it, instead of being applied.
"""

from __future__ import annotations

import json

import pytest

from integrations.external.errors import ExternalConnectionError

from tests._helpers import assign_agents_to_connection, build_identity

MASTER_KEY = "unit-test-master-key"
SECRET = "S3cretPassw0rd!"
MCP_CONFIG = {"transport": "streamable_http",
              "url": "https://mcp.example.com/mcp"}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path):
    return build_identity(tmp_path, agents=("agent-a", "agent-b"))


@pytest.fixture
def plane(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


@pytest.fixture
def backup():
    from integrations.external import backup as module
    return module


@pytest.fixture
def cutover():
    from integrations.external import cutover as module
    return module


def _configured_connection(plane, stack, *, tenant_id=None, name="MCP",
                           agents=("agent-a",)):
    """A tenant MCP narrowed to *agents*: configured, with a relation."""
    tenant = tenant_id or stack.tenant_id
    connection = plane.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=tenant,
        kind="mcp", name=name, config=dict(MCP_CONFIG), secrets={"header": "h"})
    assign_agents_to_connection(plane, tenant, connection["id"], *agents)
    return connection


def _document(backup, path):
    """The decrypted backup document, as an operator's restore would read it."""
    from auth.crypto import decrypt_secret

    with open(path, encoding="utf-8") as handle:
        return json.loads(decrypt_secret(handle.read()))


def _make_backup(backup, plane, stack, out_dir, *, scope_keys=None):
    return backup.create_backup(
        actor_user_id=stack.root, out_dir=str(out_dir), service=plane,
        scope_keys=scope_keys or ["tenant:%s" % stack.tenant_id],
        create_dir=True)


def _pre_change_backup(backup, plane, stack, out_dir):
    """A backup as it would have been written before assignment existed.

    Built by removing the two names the current writer adds, which is exactly
    what is missing from a file taken before this change: the manifest a
    pre-change deployment wrote has no assignment coverage to report, and the
    payload has no assignment tables. The ciphertext digest is untouched, so the
    backup still verifies — an old backup is a *valid* backup, which is why the
    refusal has to be an explicit check rather than an integrity failure.
    """
    report = _make_backup(backup, plane, stack, out_dir)
    manifest_path = report["backup_file"] + ".manifest.json"
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for table in backup.ASSIGNMENT_TABLES:
        manifest["table_counts"].pop(table, None)
    manifest.pop("assignment_tables", None)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, sort_keys=True)
    return report["backup_file"]


def _apply_assignment_rows(store, rows):
    """Re-apply captured rows, the way a restore runbook does.

    Written here rather than in the product because the restore of control-plane
    rows is an operator step: the backup module's job is to *capture* them
    faithfully, and that is what these tests check.
    """
    for table in ("external_connection_agent_assignment_sets",
                  "external_connection_agent_assignments"):
        columns = sorted(rows[table][0].keys()) if rows[table] else []
        for row in sorted(rows[table], key=lambda item: item["agent_id"]
                         if "agent_id" in item else item["logical_connection_id"]):
            store.execute(
                "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
                % (table, ", ".join(columns),
                   ", ".join("?" for _ in columns)),
                tuple(row[column] for column in columns))


def _clear_assignment_state(plane):
    for table in ("external_connection_agent_assignments",
                  "external_connection_agent_assignment_sets"):
        plane._store.execute("DELETE FROM %s" % table)  # noqa: SLF001


# -- coverage ----------------------------------------------------------------

def test_the_backup_carries_both_assignment_tables(backup, plane, stack,
                                                   tmp_path):
    connection = _configured_connection(plane, stack)
    report = _make_backup(backup, plane, stack, tmp_path / "backups")

    # Named in the manifest, so an operator can tell a covered backup from one
    # that predates assignment without holding the master key.
    assert list(backup.ASSIGNMENT_TABLES) == report["assignment_tables"]
    for table in backup.ASSIGNMENT_TABLES:
        assert table in report["table_counts"]

    checked = backup.verify_backup(report["backup_file"], with_key=False)
    assert checked["assignment_tables"] == list(backup.ASSIGNMENT_TABLES)

    document = _document(backup, report["backup_file"])
    sets = document["control_plane"]["external_connection_agent_assignment_sets"]
    relations = document["control_plane"][
        "external_connection_agent_assignments"]
    assert [(row["logical_connection_id"], row["configured"]) for row in sets] \
        == [(connection["id"], 1)]
    assert [row["agent_id"] for row in relations] == ["agent-a"]


def test_a_tenant_backup_carries_only_that_tenants_assignments(
        backup, plane, stack, tmp_path):
    """The relation is the tenant's, even when it names a platform template.

    Keying these tables on the connection would be wrong in both directions, so
    the scoping is checked with a second tenant that is configured in its own
    right: its rows must not travel in another tenant's backup.
    """
    other = stack.other_tenant(code="other")
    mine = _configured_connection(plane, stack, agents=("agent-a",))
    theirs = _configured_connection(
        plane, stack, tenant_id=other["tenant_id"], name="Theirs",
        agents=("other-agent",))

    report = _make_backup(backup, plane, stack, tmp_path / "backups")
    document = _document(backup, report["backup_file"])

    sets = document["control_plane"]["external_connection_agent_assignment_sets"]
    relations = document["control_plane"][
        "external_connection_agent_assignments"]
    assert {row["tenant_id"] for row in sets} == {stack.tenant_id}
    assert {row["tenant_id"] for row in relations} == {stack.tenant_id}
    assert {row["logical_connection_id"] for row in sets} == {mine["id"]}
    # The other tenant's configured connection is the thing that must not leak.
    assert theirs["id"] not in {row["logical_connection_id"] for row in sets}
    # A tenant whose own scope is asked for is covered in turn.
    theirs_backup = _make_backup(
        backup, plane, stack, tmp_path / "theirs",
        scope_keys=["tenant:%s" % other["tenant_id"]])
    their_document = _document(backup, theirs_backup["backup_file"])
    their_sets = their_document["control_plane"][
        "external_connection_agent_assignment_sets"]
    assert {row["logical_connection_id"] for row in their_sets} == {
        theirs["id"]}


# -- restore round trip ------------------------------------------------------

def test_a_restored_backup_reproduces_the_configured_restriction(
        backup, plane, stack, tmp_path):
    from integrations.external import assignment

    connection = _configured_connection(plane, stack, agents=("agent-a",))
    before = plane.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert before["configured"] is True and before["total"] == 1

    report = _make_backup(backup, plane, stack, tmp_path / "backups")
    rows = _document(backup, report["backup_file"])["control_plane"]

    # Wiped, as a fresh deployment would be before the rows are applied back.
    _clear_assignment_state(plane)
    missing = plane.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert missing["configured"] is False
    assert assignment.assignment_allows(
        plane._store, tenant_id=stack.tenant_id,  # noqa: SLF001
        logical_id=connection["id"], agent_id="agent-a")[1] \
        == assignment.REASON_STATE_MISSING

    _apply_assignment_rows(plane._store, rows)  # noqa: SLF001

    after = plane.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert after["configured"] is True
    assert after["revision"] == before["revision"]
    assert [item["id"] for item in after["items"]] \
        == [item["id"] for item in before["items"]]
    # The restriction is back, not merely the row count: an Agent outside the
    # relation is refused again while the assigned one is allowed.
    assert assignment.assignment_allows(
        plane._store, tenant_id=stack.tenant_id,  # noqa: SLF001
        logical_id=connection["id"], agent_id="agent-a") \
        == (True, assignment.REASON_ASSIGNED)
    assert assignment.assignment_allows(
        plane._store, tenant_id=stack.tenant_id,  # noqa: SLF001
        logical_id=connection["id"], agent_id="agent-b") \
        == (False, assignment.REASON_NOT_ASSIGNED)


def test_reopening_the_database_keeps_the_restriction(stack, plane, tmp_path):
    """The schema migration is re-entrant: it never re-backfills over a decision.

    Opening the store again is the real path — every process start migrates —
    and migration 35's backfill would silently flip a configured connection back
    to 沿用原权限 if it were unconditional. It is ``INSERT OR IGNORE`` on the state
    row and writes no relation, so a second pass leaves both alone.
    """
    from auth.service import IdentityService
    from integrations.external import assignment

    connection = _configured_connection(plane, stack)
    db_path = str(tmp_path / "identity.db")

    reopened = IdentityService(db_path)
    applied = [row["version"] for row in reopened._store.execute(  # noqa: SLF001
        "SELECT version FROM schema_migrations WHERE version=?",
        (len(__import__("auth.store", fromlist=["_migrations"])._migrations),))]
    assert len(applied) == 1, "migration 35 must be recorded once"

    rows = reopened._store.execute(  # noqa: SLF001
        "SELECT configured, revision FROM"
        " external_connection_agent_assignment_sets WHERE tenant_id=?"
        " AND logical_connection_id=?", (stack.tenant_id, connection["id"]))
    assert [(row["configured"], row["revision"]) for row in rows] == [(1, 1)]
    assert assignment.assigned_agent_ids(
        reopened._store, tenant_id=stack.tenant_id,
        logical_id=connection["id"]) == ["agent-a"]
    assert assignment.assignment_allows(
        reopened._store, tenant_id=stack.tenant_id,
        logical_id=connection["id"], agent_id="agent-b") \
        == (False, assignment.REASON_NOT_ASSIGNED)


# -- the incompatible restore -------------------------------------------------

def test_an_old_backup_is_refused_while_a_restriction_depends_on_it(
        backup, plane, stack, tmp_path):
    _configured_connection(plane, stack)
    path = _pre_change_backup(backup, plane, stack, tmp_path / "backups")
    # Still a valid backup: the refusal is about coverage, not corruption.
    assert backup.verify_backup(path, with_key=False)["assignment_tables"] == []

    with pytest.raises(ExternalConnectionError) as raised:
        backup.assert_restore_compatible(path, service=plane)
    assert raised.value.code == backup.BACKUP_INCOMPATIBLE_ASSIGNMENT_STATE
    assert raised.value.status == 409


def test_an_old_backup_restores_when_nothing_is_configured(
        backup, plane, stack, tmp_path):
    """No restriction to lose: the same file is accepted, and says why."""
    connection = plane.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="MCP", config=dict(MCP_CONFIG), secrets={"header": "h"})
    from tests._helpers import legacy_connection_rule
    legacy_connection_rule(plane, stack.tenant_id, connection["id"])

    path = _pre_change_backup(backup, plane, stack, tmp_path / "backups")
    report = backup.assert_restore_compatible(path, service=plane)
    assert report["restorable"] is True
    assert report["configured_sets"] == 0
    assert report["reason"] == "no_configured_assignments"


def test_a_covered_but_empty_assignment_set_is_restorable(
        backup, plane, stack, tmp_path):
    """``configured`` with nobody assigned is a real state, not missing coverage."""
    connection = _configured_connection(plane, stack, agents=())
    assert plane.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])["configured"] is True

    report = _make_backup(backup, plane, stack, tmp_path / "backups")
    checked = backup.assert_restore_compatible(
        report["backup_file"], service=plane)
    assert checked["restorable"] is True


def test_the_restore_entry_point_refuses_before_writing(cutover, backup, plane,
                                                        stack, tmp_path):
    """The refusal lands on the path an operator actually runs.

    Also on the dry run: the point of refusing is to reach the operator while the
    decision is still reversible, not to fail a write that has already begun.
    """
    _configured_connection(plane, stack)
    path = _pre_change_backup(backup, plane, stack, tmp_path / "backups")
    out_dir = tmp_path / "restored"

    with pytest.raises(ExternalConnectionError) as raised:
        cutover.restore_files(path, out_dir=str(out_dir), service=plane,
                              actor_user_id=stack.root)
    assert raised.value.code == backup.BACKUP_INCOMPATIBLE_ASSIGNMENT_STATE
    assert not out_dir.exists()


def test_a_current_backup_restores_and_reports_its_coverage(
        cutover, backup, plane, stack, tmp_path):
    _configured_connection(plane, stack)
    report = _make_backup(backup, plane, stack, tmp_path / "backups")

    preview = cutover.restore_files(
        report["backup_file"], out_dir=str(tmp_path / "restored"),
        service=plane, actor_user_id=stack.root)
    assert preview["dry_run"] is True
    assert preview["assignment_restore"]["restorable"] is True
    assert preview["assignment_restore"]["assignment_tables"] \
        == list(backup.ASSIGNMENT_TABLES)
