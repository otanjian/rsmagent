"""The platform half of a coding session: one link row, plus the cache row.

A coding conversation is *not* mirrored here. OpenCode owns the messages, the
title and whether it is running; the platform owns the association between one
of its sessions and one OpenCode session, and a small cache of the title and
timestamps so the existing history list can be sorted, searched and paged
without asking the service about every row.

Three properties make that safe to do in the conversation database rather than
a second store: the link is created in the same transaction as its cache row,
the external id is unique per service instance so a retry cannot create a
second association, and everything is scoped by the caller's own Agent, owner
and tenant exactly as the rest of the history is. A coding session also has no
messages at all, so it must list correctly with ``msg_count = 0``.
"""

import sqlite3
import time

import pytest

from agent.memory.conversation_schema import OPENCODE_SESSION_LINKS
from agent.memory.conversation_store import ConversationStore
from common.runtime_identity import identity_scope

MS = 1000


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


def _columns(store, table=OPENCODE_SESSION_LINKS):
    conn = sqlite3.connect(store._db_path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _link(store, session_id="oc_abc", **overrides):
    values = {
        "session_id": session_id,
        "external_session_id": "ses_rsm_abc",
        "service_id": "default",
        "project_dir": "/srv/checkouts/erp",
        "request_id": "req-1",
        "state": "creating",
        "title": "New session",
    }
    values.update(overrides)
    return store.create_coding_link(**values)


def _as(owner="u-1", tenant="t-1", agent=""):
    return identity_scope(agent_id=agent, user_id=owner, tenant_id=tenant)


# --- schema ---------------------------------------------------------------


def test_the_link_table_lands_in_the_conversation_database(store):
    """Same file, same connection, no second database service."""
    columns = _columns(store)
    assert columns[:2] == ["agent_id", "session_id"]
    for expected in ("service_id", "external_session_id", "project_dir",
                     "state", "request_id"):
        assert expected in columns


def test_the_link_table_keys_are_composite_and_external_id_is_unique_per_service(store):
    """The uniqueness is per service instance, which is what lets the same
    session exist on a migrated instance without a false conflict."""
    conn = sqlite3.connect(store._db_path)
    try:
        pk = [row[1] for row in conn.execute(f"PRAGMA table_info({OPENCODE_SESSION_LINKS})") if row[5]]
        indexes = list(conn.execute(f"PRAGMA index_list({OPENCODE_SESSION_LINKS})"))
    finally:
        conn.close()

    assert pk == ["agent_id", "session_id"]
    unique_column_sets = []
    conn = sqlite3.connect(store._db_path)
    try:
        for row in indexes:
            if row[2]:
                unique_column_sets.append(tuple(
                    r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})")))
    finally:
        conn.close()
    assert ("service_id", "external_session_id") in unique_column_sets


# --- creation -------------------------------------------------------------


def test_linking_prebuilds_the_cache_row_in_the_same_transaction(store):
    """A reserved link whose session row is missing would be invisible in the
    history list while still holding the external id, i.e. unreachable."""
    with _as():
        link = _link(store)

    assert link["state"] == "creating"
    assert link["project_dir"] == "/srv/checkouts/erp"
    assert link["request_id"] == "req-1"

    with _as():
        page = store.list_sessions(user_id="u-1")
    assert [s["session_id"] for s in page["sessions"]] == ["oc_abc"]
    assert page["sessions"][0]["msg_count"] == 0


def test_the_cache_row_is_owned_by_the_caller_who_reserved_it(store):
    with _as(owner="u-1"):
        _link(store)

    with _as(owner="u-2"):
        assert store.list_sessions(user_id="u-2")["sessions"] == []


def test_the_default_agent_keeps_the_empty_agent_dimension(store):
    """The link table has to follow the same convention as every other row in
    this file, or the default Agent's links would be unreachable."""
    with _as(agent=""):
        link = _link(store)

    assert link["agent_id"] == ""


def test_a_second_link_for_the_same_external_session_is_refused(store):
    """This is the constraint the whole retry story rests on: a lost response
    and a repeated request must converge on one association."""
    with _as():
        _link(store)
        with pytest.raises(sqlite3.IntegrityError):
            _link(store, session_id="oc_other", external_session_id="ses_rsm_abc")


def test_the_same_external_id_on_another_service_is_a_different_link(store):
    """After a data-instance change the ids are new again, and the old link is
    kept until the service mismatch is resolved rather than silently reused."""
    with _as():
        _link(store)
        link = _link(store, session_id="oc_other", service_id="second")

    assert link["service_id"] == "second"


def test_creation_does_not_touch_the_message_table(store):
    """Nothing is mirrored: the body stays upstream, so a coding session has
    no local messages even though it is listed."""
    with _as():
        _link(store)

    conn = sqlite3.connect(store._db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        conn.close()


# --- reads ----------------------------------------------------------------


def test_a_link_is_read_back_by_session_and_by_external_id(store):
    with _as():
        _link(store)

        by_session = store.get_coding_link("oc_abc")
        by_external = store.find_coding_link_by_external("default", "ses_rsm_abc")

    assert by_session["external_session_id"] == "ses_rsm_abc"
    assert by_session["request_id"] == "req-1"
    assert by_external["session_id"] == "oc_abc"


def test_an_unknown_link_reads_as_none(store):
    assert store.get_coding_link("oc_missing") is None
    assert store.find_coding_link_by_external("default", "ses_missing") is None


def test_links_are_listed_for_their_owner_only(store):
    """Sync must never refresh a row the caller cannot see."""
    with _as(owner="u-1"):
        _link(store, session_id="oc_one", external_session_id="ses_rsm_one")
    with _as(owner="u-2"):
        _link(store, session_id="oc_two", external_session_id="ses_rsm_two")

    with _as(owner="u-1"):
        mine = store.list_coding_links(owner="u-1", tenant_id="t-1")

    assert [link["session_id"] for link in mine["links"]] == ["oc_one"]


def test_sync_batches_walk_the_whole_set_once_with_a_stable_cursor(store):
    """The client consumes ``next_cursor`` to the end, so batches must not skip
    or repeat a row."""
    with _as():
        for index in range(5):
            _link(store, session_id=f"oc_{index:02d}",
                  external_session_id=f"ses_rsm_{index:02d}")

        first = store.list_coding_links(owner="u-1", tenant_id="t-1", limit=2)
        second = store.list_coding_links(
            owner="u-1", tenant_id="t-1", limit=2, after=first["next_cursor"])
        third = store.list_coding_links(
            owner="u-1", tenant_id="t-1", limit=2, after=second["next_cursor"])

    seen = [b["session_id"] for b in first["links"] + second["links"] + third["links"]]
    assert seen == ["oc_00", "oc_01", "oc_02", "oc_03", "oc_04"]
    assert third["next_cursor"] is None


def test_the_batch_size_defaults_to_fifty(store):
    with _as():
        for index in range(55):
            _link(store, session_id=f"oc_{index:03d}",
                  external_session_id=f"ses_rsm_{index:03d}")
        batch = store.list_coding_links(owner="u-1", tenant_id="t-1")

    assert len(batch["links"]) == 50
    assert batch["next_cursor"] == "oc_049"


# --- state and cache ------------------------------------------------------


def test_marking_ready_keeps_the_project_and_is_idempotent(store):
    with _as():
        _link(store)
        assert store.set_coding_link_state("oc_abc", "ready") is True
        assert store.set_coding_link_state("oc_abc", "ready") is True

    link = store.get_coding_link("oc_abc")
    assert (link["state"], link["project_dir"]) == ("ready", "/srv/checkouts/erp")


def test_an_unknown_link_cannot_change_state(store):
    assert store.set_coding_link_state("oc_missing", "ready") is False


def test_the_cache_follows_the_remote_title_and_time(store):
    with _as():
        _link(store)
        updated_ms = int(time.time() * MS) + 5000
        assert store.touch_coding_link_cache(
            "oc_abc", title="平台重命名", remote_updated_ms=updated_ms) is True
        session = store.list_sessions(user_id="u-1")["sessions"][0]

    # The title and the times live on the session row, not on the link: the
    # existing list, search and paging keep working untouched.
    assert session["title"] == "平台重命名"
    # The times are stored in the unit the existing list already uses.
    assert session["last_active"] == updated_ms // MS


def test_an_older_remote_result_never_overwrites_a_newer_title(store):
    """A refresh that raced a rename must not roll the title back."""
    with _as():
        _link(store)
        now_ms = int(time.time() * MS) + 10_000
        store.touch_coding_link_cache("oc_abc", title="新标题", remote_updated_ms=now_ms)
        store.touch_coding_link_cache("oc_abc", title="旧标题", remote_updated_ms=now_ms - 60_000)
        session = store.list_sessions(user_id="u-1")["sessions"][0]

    assert session["title"] == "新标题"
    assert session["last_active"] == now_ms // MS


def test_touching_a_link_never_resurrects_a_deleted_cache_row(store):
    """A late refresh arriving after a delete must not re-create the session."""
    with _as():
        _link(store)
        store.delete_coding_link("oc_abc")
        assert store.touch_coding_link_cache(
            "oc_abc", title="迟到", remote_updated_ms=int(time.time() * MS) + 1000) is False

        assert store.get_coding_link("oc_abc") is None
        assert store.list_sessions(user_id="u-1")["sessions"] == []


# --- deletion -------------------------------------------------------------


def test_deleting_a_link_removes_the_cache_row_with_it(store):
    """Both go in one transaction: a link without its cache row would hide a
    session that still holds an external id."""
    with _as():
        _link(store)
        assert store.delete_coding_link("oc_abc") is True

        assert store.get_coding_link("oc_abc") is None
        assert store.find_coding_link_by_external("default", "ses_rsm_abc") is None
        assert store.list_sessions(user_id="u-1")["sessions"] == []


def test_deleting_an_unknown_link_is_a_no_op(store):
    assert store.delete_coding_link("oc_missing") is False
