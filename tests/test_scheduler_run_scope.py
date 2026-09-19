# encoding:utf-8
"""Attribution and access tests for scheduled-run history (P5).

These run against a *real* temporary SQLite file through a real
``ConversationStore`` — never a mocked SQL layer — because the property under
test is the shape of the query itself: authorization must be part of the same
WHERE as ORDER BY/LIMIT, not a post-filter over a page of everyone's rows.
"""

import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory.conversation_store import ConversationStore
from agent.tools.scheduler.authorization import (
    ACTION_MANAGE,
    ACTION_VIEW,
    NOT_MEMBER,
    TaskAccessService,
    TaskActor,
    TaskAuthorizationError,
)
from agent.tools.scheduler.run_access import HISTORY_SCOPE, RunAccessService
from agent.tools.scheduler.run_repository import (
    RUN_NOT_FOUND,
    RUN_RUNNING,
    RUN_SCOPE_CONFLICT,
    RUN_STORE_UNAVAILABLE,
    RunGrant,
    RunQuery,
    RunScope,
    RunScopeRepository,
)

TENANT = "t1"
OTHER_TENANT = "t2"
SHARED = "shared"
BASE_TIME = 1000


# ---------------------------------------------------------------------------
# Fixtures / seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    """A real ConversationStore + repository over one temporary SQLite file."""
    store = ConversationStore(tmp_path / "index.db")
    repository = RunScopeRepository(store)
    repository.ensure_schema()
    return store, repository


def _set_started_at(store, run_id, value):
    conn = sqlite3.connect(str(store._db_path))
    try:
        conn.execute(
            "UPDATE runs SET started_at = ? WHERE run_id = ?", (int(value), run_id)
        )
        conn.commit()
    finally:
        conn.close()


def _add_run(store, repository, run_id, *, tenant=TENANT, owner="alice",
             scope="personal", agent_id=SHARED, task_id="task",
             session_id="sess", started_at=None, extras=None, record=True,
             status="done"):
    """Write a runs row and (by default) its attribution snapshot.

    ``status=None`` leaves the run open (``running``); the default closes it so
    the delete paths can be exercised without tripping the running-run guard.
    """
    assert store.create_run(
        run_id,
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        task_source="scheduler",
        extras=extras,
    )
    if started_at is not None:
        _set_started_at(store, run_id, started_at)
    if record:
        repository.record(RunScope(
            run_id=run_id, tenant_id=tenant, owner_user_id=owner, scope=scope,
            agent_id=agent_id, task_id=task_id, session_id=session_id,
        ))
    if status is not None:
        store.finish_run(run_id, status=status)


def _seed_shared_agent(store, repository):
    """One shared Agent: Alice/Bob 3 personal runs each, one public, one legacy.

    ``started_at`` is fixed so paging is deterministic, and the scope-less run
    proves legacy rows never appear in a scoped query.
    """
    for index in range(3):
        _add_run(store, repository, "alice-%d" % index, owner="alice",
                 started_at=BASE_TIME + index)
        _add_run(store, repository, "bob-%d" % index, owner="bob",
                 started_at=BASE_TIME + index)
    _add_run(store, repository, "public-0", owner="", scope="public",
             started_at=BASE_TIME + 10)
    # A run with a runs row but no snapshot: invisible to every scoped query.
    _add_run(store, repository, "legacy-0", started_at=BASE_TIME + 11,
             record=False)


def _view_grant(user, *, tenant=TENANT, agents=(SHARED,)):
    return RunGrant(
        tenant_id=tenant, user_id=user,
        personal_agent_ids=tuple(agents), public_agent_ids=tuple(agents),
    )


def _manage_grant(user, *, tenant=TENANT, agents=(SHARED,)):
    return RunGrant(
        tenant_id=tenant, user_id=user,
        personal_agent_ids=tuple(agents), public_agent_ids=tuple(agents),
    )


def _service(repository, *, bound=(SHARED,), actor=None, audit=None,
             scope_resolver=None):
    access = TaskAccessService(
        store_resolver=lambda actor_, agent_id: None,
        agent_ids=lambda actor_: list(bound),
        scope_resolver=scope_resolver,
    )
    holder = {"actor": actor or TaskActor(user_id="alice", tenant_id=TENANT)}
    service = RunAccessService(
        repository, access, lambda: holder["actor"],
        lambda actor_: list(bound), audit=audit,
    )
    return service, holder


# ---------------------------------------------------------------------------
# The first repository case: authorization before pagination
# ---------------------------------------------------------------------------


def test_list_visible_paginates_within_alice_authorized_rows(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    grant = _view_grant("alice")

    page1 = repository.list_visible(grant, RunQuery(limit=2, offset=0))
    page2 = repository.list_visible(grant, RunQuery(limit=2, offset=2))
    page3 = repository.list_visible(grant, RunQuery(limit=2, offset=4))

    # Authorized = Alice's 3 personal rows + the public row. Bob's rows and the
    # scope-less legacy row never appear, and paging is over the authorized set.
    assert [row["run_id"] for row in page1] == ["public-0", "alice-2"]
    assert [row["run_id"] for row in page2] == ["alice-1", "alice-0"]
    assert page3 == []


def test_list_visible_excludes_a_foreign_members_rows(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    bob_grant = _view_grant("bob")

    rows = repository.list_visible(bob_grant, RunQuery(limit=10))
    ids = {row["run_id"] for row in rows}
    # Bob sees the public run and his own; not one of Alice's, not the legacy.
    assert ids == {"public-0", "bob-0", "bob-1", "bob-2"}


def test_get_visible_foreign_run_is_none(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    assert repository.get_visible(_view_grant("alice"), "bob-0") is None
    assert repository.get_visible(_view_grant("alice"), "legacy-0") is None
    assert repository.get_visible(_view_grant("alice"), "alice-0") is not None


def test_delete_visible_foreign_run_is_not_found(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.delete_visible(lambda: _manage_grant("alice"), "bob-0")
    assert captured.value.code == RUN_NOT_FOUND
    assert captured.value.status == 404
    # The foreign row is untouched.
    assert store.get_run("bob-0") is not None


def test_admin_cannot_read_another_members_personal_runs(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    # An admin's grant has every Agent but still pins owner=admin in the
    # personal branch, so Alice's private rows never match.
    admin_grant = _view_grant("admin")
    ids = {row["run_id"] for row in repository.list_visible(admin_grant, RunQuery())}
    assert ids == {"public-0"}
    assert repository.get_visible(admin_grant, "alice-0") is None


def test_public_view_is_open_but_manage_needs_the_admin_predicate(env):
    store, repository = env
    _seed_shared_agent(store, repository)

    # A plain member may view the public run...
    member_view = _view_grant("bob")
    assert repository.get_visible(member_view, "public-0") is not None
    # ...but a manage grant with no public Agent set may not touch it.
    member_manage = RunGrant(
        tenant_id=TENANT, user_id="bob",
        personal_agent_ids=(SHARED,), public_agent_ids=(),
    )
    assert repository.get_visible(member_manage, "public-0") is None
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.delete_visible(lambda: member_manage, "public-0")
    assert captured.value.code == RUN_NOT_FOUND

    # The administrator's grant carries the public Agent, so it may delete it.
    admin_manage = _view_grant("admin")
    deleted = repository.delete_visible(lambda: admin_manage, "public-0")
    assert deleted["run_id"] == "public-0"
    assert store.get_run("public-0") is None


def test_equal_started_at_orders_by_run_id_descending(env):
    store, repository = env
    _add_run(store, repository, "aaa", started_at=BASE_TIME)
    _add_run(store, repository, "zzz", started_at=BASE_TIME)
    _add_run(store, repository, "mmm", started_at=BASE_TIME)

    rows = repository.list_visible(_view_grant("alice"), RunQuery())
    assert [row["run_id"] for row in rows] == ["zzz", "mmm", "aaa"]


def test_optional_filters_narrow_the_authorized_query(env):
    store, repository = env
    _add_run(store, repository, "t-1-run", task_id="t-1", started_at=100)
    _add_run(store, repository, "t-2-run", task_id="t-2", started_at=200)
    grant = _view_grant("alice")

    by_task = repository.list_visible(grant, RunQuery(task_id="t-2"))
    assert [row["run_id"] for row in by_task] == ["t-2-run"]

    by_since = repository.list_visible(grant, RunQuery(since=150))
    assert [row["run_id"] for row in by_since] == ["t-2-run"]
    # ``since`` is inclusive: started_at >= since.
    assert len(repository.list_visible(grant, RunQuery(since=100))) == 2

    by_agent = repository.list_visible(grant, RunQuery(agent_id=SHARED))
    assert len(by_agent) == 2
    # A different Agent narrows to nothing (and never widens the grant).
    assert repository.list_visible(grant, RunQuery(agent_id="other")) == []


def test_empty_grant_agent_sets_match_nothing(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    empty = RunGrant(
        tenant_id=TENANT, user_id="alice",
        personal_agent_ids=(), public_agent_ids=(),
    )
    assert repository.list_visible(empty, RunQuery()) == []
    assert repository.get_visible(empty, "alice-0") is None


def test_run_query_normalizes_bounds():
    assert RunQuery(limit=0).limit == 1
    assert RunQuery(limit=-5).limit == 1
    assert RunQuery(limit=9999).limit == 500
    assert RunQuery(limit="bad").limit == 100
    assert RunQuery(offset=-3).offset == 0
    assert RunQuery(since=-1).since is None
    assert RunQuery(agent_id="  a  ", task_id=" t ").agent_id == "a"
    assert RunQuery(agent_id="  a  ").task_id == ""


# ---------------------------------------------------------------------------
# Schema / record semantics
# ---------------------------------------------------------------------------


def test_ensure_schema_and_record_are_idempotent(env):
    store, repository = env
    _add_run(store, repository, "r1")  # record called once inside
    repository.ensure_schema()
    repository.ensure_schema()
    scope = RunScope(
        run_id="r1", tenant_id=TENANT, owner_user_id="alice", scope="personal",
        agent_id=SHARED, task_id="task", session_id="sess",
    )
    repository.record(scope)
    repository.record(scope)

    with sqlite3.connect(str(store._db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM fork_scheduler_run_scopes WHERE run_id = 'r1'"
        ).fetchone()[0]
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='fork_scheduler_run_scopes'"
            )
        }
    assert count == 1
    assert {"idx_fork_run_scope_owner", "idx_fork_run_scope_public"} <= indexes


def test_record_refuses_to_rewrite_ownership(env):
    store, repository = env
    _add_run(store, repository, "r1", owner="alice")

    # Same run_id, different owner: refused, and the stored snapshot stands.
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.record(RunScope(
            run_id="r1", tenant_id=TENANT, owner_user_id="bob",
            scope="personal", agent_id=SHARED, task_id="task",
            session_id="sess",
        ))
    assert captured.value.code == RUN_SCOPE_CONFLICT
    assert captured.value.status == 409

    rows = repository.list_visible(_view_grant("alice"), RunQuery())
    assert [row["run_id"] for row in rows] == ["r1"]
    assert repository.list_visible(_view_grant("bob"), RunQuery()) == []


def test_record_refuses_when_it_disagrees_with_the_runs_row(env):
    store, repository = env
    # A runs row for a different Agent than the snapshot claims.
    store.create_run("r1", agent_id="other", session_id="sess",
                     task_id="task", task_source="scheduler")
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.record(RunScope(
            run_id="r1", tenant_id=TENANT, owner_user_id="alice",
            scope="personal", agent_id=SHARED, task_id="task",
            session_id="sess",
        ))
    assert captured.value.code == RUN_SCOPE_CONFLICT

    # A non-scheduler run is never attributed as a scheduler run.
    store.create_run("r2", agent_id=SHARED, session_id="sess",
                     task_id="task", task_source="native")
    with pytest.raises(TaskAuthorizationError):
        repository.record(RunScope(
            run_id="r2", tenant_id=TENANT, owner_user_id="alice",
            scope="personal", agent_id=SHARED, task_id="task",
            session_id="sess",
        ))


def test_record_without_a_runs_row_is_refused(env):
    store, repository = env
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.record(RunScope(
            run_id="ghost", tenant_id=TENANT, owner_user_id="alice",
            scope="personal", agent_id=SHARED, task_id="task",
            session_id="sess",
        ))
    assert captured.value.code == RUN_SCOPE_CONFLICT


def test_record_rejects_a_personal_scope_without_owner(env):
    store, repository = env
    store.create_run("r1", agent_id=SHARED, session_id="sess",
                     task_id="task", task_source="scheduler")
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.record(RunScope(
            run_id="r1", tenant_id=TENANT, owner_user_id="", scope="personal",
            agent_id=SHARED, task_id="task", session_id="sess",
        ))
    assert captured.value.code == RUN_SCOPE_CONFLICT


def test_ensure_schema_failure_is_loud(env, monkeypatch):
    store, repository = env

    def _boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_connect", _boom)
    with pytest.raises(TaskAuthorizationError) as captured:
        repository.ensure_schema()
    assert captured.value.code == RUN_STORE_UNAVAILABLE
    assert captured.value.status == 503


# ---------------------------------------------------------------------------
# Delete: transaction, status conflict, rollback
# ---------------------------------------------------------------------------


def test_delete_visible_removes_both_tables_and_keeps_messages(env):
    store, repository = env
    _add_run(store, repository, "r1", session_id="sess-1")
    store.append_messages("sess-1", [{"role": "user", "content": "keep me"}])

    deleted = repository.delete_visible(lambda: _manage_grant("alice"), "r1")
    assert deleted["run_id"] == "r1"
    assert deleted["task_id"] == "task"

    with sqlite3.connect(str(store._db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = 'r1'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM fork_scheduler_run_scopes WHERE run_id = 'r1'"
        ).fetchone()[0] == 0
        # The delivered message is a different resource and is never deleted.
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 'sess-1'"
        ).fetchone()[0] == 1


def test_delete_visible_refuses_a_running_run(env):
    store, repository = env
    _add_run(store, repository, "r1", status=None)  # left ``running``

    with pytest.raises(TaskAuthorizationError) as captured:
        repository.delete_visible(lambda: _manage_grant("alice"), "r1")
    assert captured.value.code == RUN_RUNNING
    assert captured.value.status == 409
    assert store.get_run("r1") is not None
    assert repository.get_visible(_view_grant("alice"), "r1") is not None


def test_delete_visible_rolls_back_on_resolver_failure(env):
    store, repository = env
    _add_run(store, repository, "r1")
    store.finish_run("r1", status="done")

    def _revoked():
        raise TaskAuthorizationError(NOT_MEMBER, status=403)

    with pytest.raises(TaskAuthorizationError) as captured:
        repository.delete_visible(_revoked, "r1")
    assert captured.value.code == NOT_MEMBER
    # Both tables are consistent: nothing was deleted by the aborted attempt.
    assert store.get_run("r1") is not None
    assert repository.get_visible(_view_grant("alice"), "r1") is not None


def test_delete_visible_rolls_back_on_unexpected_error(env):
    store, repository = env
    _add_run(store, repository, "r1")
    store.finish_run("r1", status="done")

    def _explode():
        raise RuntimeError("resolver blew up")

    with pytest.raises(TaskAuthorizationError) as captured:
        repository.delete_visible(_explode, "r1")
    assert captured.value.code == RUN_STORE_UNAVAILABLE
    assert store.get_run("r1") is not None
    assert repository.get_visible(_view_grant("alice"), "r1") is not None


def test_concurrent_double_delete_leaves_one_winner(env):
    store, repository = env
    _add_run(store, repository, "r1")
    store.finish_run("r1", status="done")

    outcomes = []
    barrier = threading.Barrier(2)

    def _delete():
        barrier.wait()
        try:
            repository.delete_visible(lambda: _manage_grant("alice"), "r1")
            outcomes.append("deleted")
        except TaskAuthorizationError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=_delete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)

    assert sorted(outcomes) == sorted(["deleted", RUN_NOT_FOUND])
    assert store.get_run("r1") is None


# ---------------------------------------------------------------------------
# RunAccessService: grants re-derived per request
# ---------------------------------------------------------------------------


def test_service_list_projects_only_whitelisted_fields(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    service, _ = _service(repository)

    body = service.list_runs(RunQuery(limit=2))
    assert body["status"] == "success"
    assert body["history_scope"] == HISTORY_SCOPE
    assert [row["run_id"] for row in body["runs"]] == ["public-0", "alice-2"]

    row = body["runs"][1]
    # The raw extras sidecar and the internal scope aliases never reach a client.
    assert "extras" not in row
    assert "scope_tenant_id" not in row
    assert "scope_owner_user_id" not in row
    assert "run_scope" not in row
    for field in ("task_name", "action_type", "channel_type", "instance_id",
                  "trigger", "output_preview"):
        assert field in row


def test_service_get_run_preview_without_body(env):
    store, repository = env
    _add_run(store, repository, "r1",
             extras={"output_preview": "peek", "task_name": "Digest"})
    service, _ = _service(repository)

    detail = service.get_run("r1")
    assert detail["output_preview"] == "peek"
    assert detail["task_name"] == "Digest"
    # P6 owns the safe session-scoped body read.
    assert detail["full_output"] is None


def test_service_get_and_delete_foreign_run_404(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    service, _ = _service(repository)

    with pytest.raises(TaskAuthorizationError) as captured:
        service.get_run("bob-0")
    assert captured.value.code == RUN_NOT_FOUND
    assert captured.value.status == 404

    with pytest.raises(TaskAuthorizationError) as captured:
        service.delete_run("bob-0")
    assert captured.value.code == RUN_NOT_FOUND
    assert store.get_run("bob-0") is not None


def test_service_delete_owns_run_and_audits_ids_only(env):
    store, repository = env
    _add_run(store, repository, "r1", task_id="task-7", session_id="sess-1")
    store.finish_run("r1", status="done")
    calls = []

    def _audit(**kwargs):
        calls.append(kwargs)

    service, _ = _service(repository, audit=_audit)
    service.delete_run("r1")

    assert store.get_run("r1") is None
    assert calls and calls[-1]["result"] == "success"
    assert calls[-1]["run_id"] == "r1"
    assert calls[-1]["task_id"] == "task-7"
    # Auditing never carries content: only the id fields and the outcome.
    assert "content" not in calls[-1]
    assert "output_preview" not in calls[-1]


def test_service_public_manage_requires_admin(env):
    store, repository = env
    _seed_shared_agent(store, repository)

    bob = TaskActor(user_id="bob", tenant_id=TENANT)
    bob_service, _ = _service(repository, actor=bob)
    # Bob may see the public run...
    assert any(row["run_id"] == "public-0"
               for row in bob_service.list_runs(RunQuery())["runs"])
    # ...but his manage grant excludes it.
    with pytest.raises(TaskAuthorizationError) as captured:
        bob_service.delete_run("public-0")
    assert captured.value.code == RUN_NOT_FOUND

    admin = TaskActor(user_id="admin", tenant_id=TENANT, is_tenant_admin=True)
    admin_service, _ = _service(repository, actor=admin)
    admin_service.delete_run("public-0")
    assert store.get_run("public-0") is None
    # The admin never gained access to Alice's private row along the way.
    with pytest.raises(TaskAuthorizationError):
        admin_service.get_run("alice-0")


def test_service_agent_use_revocation_keeps_owner_history(env):
    store, repository = env
    _add_run(store, repository, "r1", owner="alice")

    def _denied_scope(actor, agent_id):
        return {"agent_id": agent_id, "bound_tenant": TENANT, "can_use": False}

    alice = TaskActor(user_id="alice", tenant_id=TENANT)
    service, _ = _service(repository, actor=alice, scope_resolver=_denied_scope)
    # Personal history is an ownership right, not a use of the Agent.
    assert [row["run_id"] for row in service.list_runs(RunQuery())["runs"]] == ["r1"]
    service.delete_run("r1")
    assert store.get_run("r1") is None


def test_service_permission_revocation_hides_history(env):
    store, repository = env
    _seed_shared_agent(store, repository)

    # No Agent is bound to the tenant any more: both grant sets are empty.
    service, _ = _service(repository, bound=())
    assert service.list_runs(RunQuery()) == {
        "status": "success", "runs": [], "history_scope": HISTORY_SCOPE,
    }
    with pytest.raises(TaskAuthorizationError) as captured:
        service.get_run("alice-0")
    assert captured.value.code == RUN_NOT_FOUND


def test_service_tenant_deactivation_is_refused(env):
    store, repository = env
    _seed_shared_agent(store, repository)
    # must_change_password makes is_member False, exactly as the task service
    # treats it: no grant at all, rather than an empty history.
    actor = TaskActor(user_id="alice", tenant_id=TENANT, must_change_password=True)
    service, _ = _service(repository, actor=actor)

    with pytest.raises(TaskAuthorizationError) as captured:
        service.list_runs(RunQuery())
    assert captured.value.code == NOT_MEMBER
    with pytest.raises(TaskAuthorizationError) as captured:
        service.get_run("alice-0")
    assert captured.value.code == NOT_MEMBER
    with pytest.raises(TaskAuthorizationError) as captured:
        service.delete_run("alice-0")
    assert captured.value.code == NOT_MEMBER


def test_service_agent_rebinding_does_not_migrate_old_snapshots(env):
    store, repository = env
    _add_run(store, repository, "r1", tenant=TENANT, owner="alice")

    # A member of the tenant the Agent moved to must not inherit the history.
    other = TaskActor(user_id="carol", tenant_id=OTHER_TENANT)
    other_service, _ = _service(repository, bound=(SHARED,), actor=other)
    assert other_service.list_runs(RunQuery())["runs"] == []
    with pytest.raises(TaskAuthorizationError):
        other_service.get_run("r1")

    # The original tenant keeps the snapshot only while the Agent is still
    # bound there; once it is not, there is no current basis to grant access.
    unbound, _ = _service(repository, bound=("other-agent",))
    assert unbound.list_runs(RunQuery())["runs"] == []


def test_service_task_deletion_does_not_downgrade_attribution(env):
    store, repository = env
    # The task is already "deleted": nothing here consults a task store.
    _add_run(store, repository, "r1", owner="alice", task_id="deleted-task")

    alice_service, _ = _service(repository)
    assert [row["run_id"] for row in alice_service.list_runs(RunQuery())["runs"]] == ["r1"]

    # An admin's public grant must not turn the personal snapshot public.
    admin = TaskActor(user_id="admin", tenant_id=TENANT, is_tenant_admin=True)
    admin_service, _ = _service(repository, actor=admin)
    assert admin_service.list_runs(RunQuery())["runs"] == []


def test_service_returns_empty_list_rather_than_raising_on_no_rows(env):
    store, repository = env
    service, _ = _service(repository)
    assert service.list_runs(RunQuery())["runs"] == []


# ---------------------------------------------------------------------------
# Integration: _record_scheduler_run attribute-on-write
# ---------------------------------------------------------------------------


@pytest.fixture
def one_agent(tmp_path):
    """Pin one registry with a ``default`` Agent so the store resolves."""
    from agent.memory import clear_conversation_store_cache
    from agent.registry import AgentProfile, AgentRegistry, set_agent_registry

    registry = AgentRegistry(
        [AgentProfile("default", "Default", str(tmp_path / "default"))],
        default_agent_id="default",
    )
    set_agent_registry(registry)
    clear_conversation_store_cache()
    try:
        yield registry
    finally:
        set_agent_registry(None)
        clear_conversation_store_cache()


def _personal_task():
    return {
        "id": "task-42",
        "name": "Daily digest",
        "scope": "personal",
        "owner": {
            "user_id": "alice", "tenant_id": TENANT,
            "agent_id": "default", "session_id": "sess-1",
        },
        "action": {
            "type": "send_message", "channel_type": "feishu",
            "receiver": "u-1", "notify_session_id": "sess-1", "content": "hi",
        },
    }


def _public_task(scope="public"):
    return {
        "id": "task-public",
        "name": "Agent digest",
        "scope": scope,
        "action": {
            "type": "send_message", "channel_type": "feishu",
            "receiver": "u-1", "notify_session_id": "sess-p", "content": "hi",
        },
    }


def test_integration_public_task_records_public_scope(monkeypatch, one_agent):
    import auth.service as auth_service

    from agent.memory import get_conversation_store
    from agent.tools.scheduler import integration

    class _Bound:
        def get_agent_binding(self, agent_id):
            return {"tenant_id": TENANT}

    monkeypatch.setattr(auth_service, "get_identity_service", lambda: _Bound())
    monkeypatch.setattr(integration, "_is_channel_ready", lambda *a, **k: True)
    monkeypatch.setattr(integration, "_execute_send_message", lambda *a, **k: True)

    ok = integration._run_scheduled_task(
        _public_task(), agent_bridge=object(), agent_id="default")
    assert ok is True

    store = get_conversation_store()
    rows = RunScopeRepository(store).list_visible(
        RunGrant(tenant_id=TENANT, user_id="carol",
                 personal_agent_ids=(), public_agent_ids=("default",)),
        RunQuery(),
    )
    assert [row["run_id"] for row in rows] == [
        store.list_runs(task_source="scheduler")[0]["run_id"]
    ]
    assert rows[0]["run_scope"] == "public"
    assert rows[0]["scope_owner_user_id"] == ""


def test_integration_legacy_unattributed_task_stays_invisible(monkeypatch, one_agent):
    from agent.memory import get_conversation_store
    from agent.tools.scheduler import integration

    monkeypatch.setattr(integration, "_is_channel_ready", lambda *a, **k: True)
    monkeypatch.setattr(integration, "_execute_send_message", lambda *a, **k: True)

    # No owner and no explicit scope: merely "implied public" is not evidence,
    # so the run gets no snapshot and cannot be claimed by anyone.
    integration._run_scheduled_task(
        _public_task(scope=""), agent_bridge=object(), agent_id="default")

    store = get_conversation_store()
    run = store.list_runs(task_source="scheduler")[0]
    assert run["agent_id"] == "default"
    repository = RunScopeRepository(store)
    repository.ensure_schema()
    for grant in (
        RunGrant(tenant_id=TENANT, user_id="alice",
                 personal_agent_ids=("default",), public_agent_ids=("default",)),
        RunGrant(tenant_id=TENANT, user_id="admin",
                 personal_agent_ids=("default",), public_agent_ids=("default",)),
    ):
        assert repository.list_visible(grant, RunQuery()) == []


def test_integration_writes_business_agent_id_and_scope(monkeypatch, one_agent):
    from agent.memory import get_conversation_store
    from agent.tools.scheduler import integration
    from common.runtime_identity import identity_scope

    monkeypatch.setattr(integration, "_is_channel_ready", lambda *a, **k: True)
    monkeypatch.setattr(integration, "_execute_send_message", lambda *a, **k: True)

    with identity_scope(user_id="alice", tenant_id=TENANT):
        ok = integration._run_scheduled_task(
            _personal_task(), agent_bridge=object(), agent_id="default")
    assert ok is True

    store = get_conversation_store()
    run = store.list_runs(task_source="scheduler")[0]
    assert run["agent_id"] == "default"  # the real business id, not ""
    assert run["status"] == "done"

    repository = RunScopeRepository(store)
    rows = repository.list_visible(
        RunGrant(tenant_id=TENANT, user_id="alice",
                 personal_agent_ids=("default",), public_agent_ids=()),
        RunQuery(),
    )
    assert [row["run_id"] for row in rows] == [run["run_id"]]
    assert rows[0]["scope_owner_user_id"] == "alice"
    assert rows[0]["run_scope"] == "personal"


def test_integration_scope_failure_does_not_block_delivery_or_finish(
        monkeypatch, one_agent):
    from agent.memory import get_conversation_store
    from agent.tools.scheduler import integration
    from agent.tools.scheduler import run_repository
    from common.runtime_identity import identity_scope

    deliveries = []

    def _deliver(*args, **kwargs):
        deliveries.append(1)
        return True

    def _boom(self, scope):
        raise RuntimeError("scope store down")

    monkeypatch.setattr(integration, "_is_channel_ready", lambda *a, **k: True)
    monkeypatch.setattr(integration, "_execute_send_message", _deliver)
    monkeypatch.setattr(run_repository.RunScopeRepository, "record", _boom)

    with identity_scope(user_id="alice", tenant_id=TENANT):
        ok = integration._run_scheduled_task(
            _personal_task(), agent_bridge=object(), agent_id="default")

    # Delivery happened exactly once (no re-dispatch) and the run was still
    # closed by finish_run despite the attribution failure.
    assert ok is True
    assert deliveries == [1]
    run = get_conversation_store().list_runs(task_source="scheduler")[0]
    assert run["status"] == "done"
    assert run["ended_at"] is not None
    # The failed write left no scope: the run is invisible, never unscoped.
    assert run_repository.RunScopeRepository(
        get_conversation_store()).list_visible(
        RunGrant(tenant_id=TENANT, user_id="alice",
                 personal_agent_ids=("default",), public_agent_ids=()),
        RunQuery(),
    ) == []


def test_integration_process_exit_between_writes_leaves_invisible_row(
        monkeypatch, one_agent):
    from agent.tools.scheduler import integration, run_repository
    from common.runtime_identity import identity_scope

    monkeypatch.setattr(integration, "_is_channel_ready", lambda *a, **k: True)
    monkeypatch.setattr(integration, "_execute_send_message", lambda *a, **k: True)
    # Simulate the process dying after create_run but before record(): the
    # scope write never lands, so only the invisible runs row remains.
    monkeypatch.setattr(
        run_repository.RunScopeRepository, "record",
        lambda self, scope: None,
    )

    with identity_scope(user_id="alice", tenant_id=TENANT):
        result = integration._record_scheduler_run(_personal_task(), "default")
    assert result is not None
    store, run_id = result
    assert store.get_run(run_id) is not None

    repository = run_repository.RunScopeRepository(store)
    repository.ensure_schema()
    assert repository.list_visible(
        RunGrant(tenant_id=TENANT, user_id="alice",
                 personal_agent_ids=("default",), public_agent_ids=()),
        RunQuery(),
    ) == []
