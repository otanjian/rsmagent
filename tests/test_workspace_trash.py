"""Writable scope, delete protection and the recycle bin.

The console's delete is a move into ``user/<uid>/.trash``, so the interesting
properties are not "does the file go away" but: which paths are refused, that a
refusal costs nothing, that a restore lands back where it came from, and that
the bin itself is never listed as if it were user content.
"""

import json
import os
import time

import pytest

from agent.workspace import service as service_module
from agent.workspace.service import (
    WorkspaceService,
    join_rel,
    undeletable_reason,
)


OWNER = "u-alice"


def _service(tmp_path):
    return WorkspaceService(str(tmp_path))


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _item(svc, rel):
    """The shape the delete handler hands the service."""
    return {
        "rel": rel,
        "kind": "directory" if svc.is_dir(rel) else "file",
        "size": svc.entry_size(rel),
    }


# ----------------------------------------------------------------------
# join_rel
# ----------------------------------------------------------------------
def test_join_rel_normalizes_separators_and_stray_slashes():
    assert join_rel("user/u-alice", "docs/a.txt") == "user/u-alice/docs/a.txt"
    # A directory is a position the console already listed, so a stray slash is
    # sloppy input rather than an escape attempt.
    assert join_rel("/user/u-alice/", "docs/") == "user/u-alice/docs"
    assert join_rel("/", "a.txt") == "a.txt"
    assert join_rel("", "a.txt") == "a.txt"
    assert join_rel("a\\b", "c") == "a/b/c"


@pytest.mark.parametrize("directory,relative", [
    ("user/u-alice", "../u-bob/secret.txt"),
    ("user/u-alice", "/etc/passwd"),
    ("user/u-alice", ".."),
    ("", ".."),
    ("user/u-alice", "docs/../../bob"),
])
def test_join_rel_refuses_escapes(directory, relative):
    from common.safe_fs import UnsafePathError

    with pytest.raises(UnsafePathError):
        join_rel(directory, relative)


# ----------------------------------------------------------------------
# undeletable_reason
# ----------------------------------------------------------------------
@pytest.mark.parametrize("rel", sorted(service_module.UNDELETABLE_ROOT_ENTRIES))
def test_agent_internals_are_protected_at_the_root(rel):
    assert undeletable_reason(rel) == "agent_internal"
    # ...and inside them, since deleting a file out of `memory/` breaks the
    # Agent just as thoroughly as deleting `memory/`.
    assert undeletable_reason(rel + "/inner.txt") == "agent_internal"


def test_same_name_inside_the_users_own_directory_is_not_protected():
    """The rule matches the path's first component only.

    ``user/u-alice/memory`` is the *user's* folder that happens to be called
    ``memory``; treating it as the Agent's memory would make a whole class of
    legitimate deletions impossible.
    """
    assert undeletable_reason("user/u-alice/memory") is None
    assert undeletable_reason("user/u-alice/memory/notes.md") is None
    assert undeletable_reason("user/u-alice/knowledge/a.md") is None


def test_user_container_and_own_directory_are_protected():
    assert undeletable_reason("user") == "user_container"
    assert undeletable_reason("user/u-alice") == "user_container"
    # A member's subtree beneath their own directory is ordinary content.
    assert undeletable_reason("user/u-alice/docs/a.txt") is None


def test_trash_is_never_a_delete_target():
    assert undeletable_reason(".trash") == "trash_not_targetable"
    assert undeletable_reason(".trash/b/files/a.txt") == "trash_not_targetable"
    assert undeletable_reason(
        "user/u-alice/.trash/b/files/a.txt") == "trash_not_targetable"


def test_undeletable_reason_validates_before_classifying():
    from common.safe_fs import UnsafePathError

    with pytest.raises(UnsafePathError):
        undeletable_reason("../AGENT.md")


# ----------------------------------------------------------------------
# write_bytes / ensure_dir
# ----------------------------------------------------------------------
def test_write_bytes_creates_parents_and_reports_size(tmp_path):
    svc = _service(tmp_path)

    result = svc.write_bytes("user/u-alice/docs/deep/a.bin", b"\x00\x01\x02")

    assert result == {"path": "user/u-alice/docs/deep/a.bin", "size": 3}
    assert (tmp_path / "user/u-alice/docs/deep/a.bin").read_bytes() == b"\x00\x01\x02"


def test_write_bytes_refuses_a_symlinked_parent(tmp_path):
    from common.safe_fs import UnsafePathError

    outside = tmp_path / "outside"
    outside.mkdir()
    user_dir = tmp_path / "user" / OWNER
    user_dir.mkdir(parents=True)
    try:
        os.symlink(str(outside), str(user_dir / "sneak"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this platform")
    svc = _service(tmp_path)

    with pytest.raises(UnsafePathError):
        svc.write_bytes("user/%s/sneak/planted.txt" % OWNER, b"boom")
    assert not (outside / "planted.txt").exists()


def test_ensure_dir_is_idempotent(tmp_path):
    svc = _service(tmp_path)

    assert svc.ensure_dir("user/u-alice/docs") == "user/u-alice/docs"
    svc.ensure_dir("user/u-alice/docs")
    assert (tmp_path / "user/u-alice/docs").is_dir()


# ----------------------------------------------------------------------
# move_to_trash
# ----------------------------------------------------------------------
def test_delete_moves_a_file_into_the_bin(tmp_path):
    _write(tmp_path / "user" / OWNER / "notes.md", "hello")
    svc = _service(tmp_path)

    result = svc.move_to_trash(OWNER, [_item(svc, "user/%s/notes.md" % OWNER)])

    assert result["failed"] == []
    assert len(result["moved"]) == 1
    assert not (tmp_path / "user" / OWNER / "notes.md").exists()
    payload = (tmp_path / "user" / OWNER / ".trash" / result["batch_id"]
               / "files" / "0")
    assert payload.read_text(encoding="utf-8") == "hello"


def test_delete_moves_a_whole_directory_in_one_rename(tmp_path):
    _write(tmp_path / "user" / OWNER / "proj" / "a.txt", "a")
    _write(tmp_path / "user" / OWNER / "proj" / "sub" / "b.txt", "b")
    svc = _service(tmp_path)

    result = svc.move_to_trash(OWNER, [_item(svc, "user/%s/proj" % OWNER)])

    assert result["failed"] == []
    assert not (tmp_path / "user" / OWNER / "proj").exists()
    payload = (tmp_path / "user" / OWNER / ".trash" / result["batch_id"]
               / "files" / "0")
    assert (payload / "sub" / "b.txt").read_text(encoding="utf-8") == "b"


def test_one_request_is_one_batch_directory(tmp_path):
    """Deleting many items must not mint one bookkeeping directory per item."""
    for n in range(5):
        _write(tmp_path / "user" / OWNER / ("f%d.txt" % n))
    svc = _service(tmp_path)

    result = svc.move_to_trash(
        OWNER, [_item(svc, "user/%s/f%d.txt" % (OWNER, n)) for n in range(5)])

    assert len(result["moved"]) == 5
    batches = os.listdir(tmp_path / "user" / OWNER / ".trash")
    assert batches == [result["batch_id"]]


def test_a_failed_item_does_not_undo_the_others(tmp_path):
    _write(tmp_path / "user" / OWNER / "here.txt")
    svc = _service(tmp_path)

    result = svc.move_to_trash(OWNER, [
        _item(svc, "user/%s/here.txt" % OWNER),
        {"rel": "user/%s/gone.txt" % OWNER, "kind": "file", "size": 0},
    ])

    assert [m["rel"] for m in result["moved"]] == ["user/%s/here.txt" % OWNER]
    assert result["failed"] == [{"rel": "user/%s/gone.txt" % OWNER,
                                 "code": "not_found"}]
    assert not (tmp_path / "user" / OWNER / "here.txt").exists()


def test_a_wholly_failed_batch_leaves_no_empty_shell(tmp_path):
    svc = _service(tmp_path)

    result = svc.move_to_trash(OWNER, [
        {"rel": "user/%s/nope.txt" % OWNER, "kind": "file", "size": 0}])

    assert result["batch_id"] is None
    assert result["moved"] == []
    # No bin at all: the first thing a wholly-failed delete does is leave marks.
    assert not (tmp_path / "user" / OWNER / ".trash").exists()


def test_delete_refuses_an_escaping_path(tmp_path):
    """A per-item escape is that item's failure, never the batch's.

    One malformed entry among five thousand must not abort the request, and the
    file it named must still be where it was.
    """
    _write(tmp_path / "secret.txt", "s")
    svc = _service(tmp_path)

    result = svc.move_to_trash(OWNER, [{"rel": "../secret.txt", "kind": "file"}])

    assert result["batch_id"] is None
    assert result["failed"] == [{"rel": "../secret.txt", "code": "unsafe_path"}]
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "s"


# ----------------------------------------------------------------------
# list_trash
# ----------------------------------------------------------------------
def test_list_trash_reports_what_is_still_held(tmp_path):
    _write(tmp_path / "user" / OWNER / "a.txt", "aaa")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/a.txt" % OWNER)])

    entries = svc.list_trash(OWNER)

    assert len(entries) == 1
    assert entries[0]["rel"] == "user/%s/a.txt" % OWNER
    assert entries[0]["kind"] == "file"
    assert entries[0]["size"] == 3
    assert entries[0]["index"] == 0


def test_list_trash_is_empty_without_a_bin(tmp_path):
    assert _service(tmp_path).list_trash(OWNER) == []


# ----------------------------------------------------------------------
# restore_from_trash
# ----------------------------------------------------------------------
def test_restore_puts_a_file_back_where_it_was(tmp_path):
    _write(tmp_path / "user" / OWNER / "docs" / "a.txt", "body")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/docs/a.txt" % OWNER)])

    result = svc.restore_from_trash(OWNER, svc.list_trash(OWNER)[0]["batch_id"])

    assert result["failed"] == []
    assert result["restored"][0]["path"] == "user/%s/docs/a.txt" % OWNER
    assert result["restored"][0]["renamed"] is False
    assert (tmp_path / "user" / OWNER / "docs" / "a.txt").read_text(
        encoding="utf-8") == "body"
    assert svc.list_trash(OWNER) == []


def test_restore_renames_rather_than_clobbering(tmp_path):
    """The change ships no move operation, so a taken path must not dead-end."""
    _write(tmp_path / "user" / OWNER / "a.txt", "old")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/a.txt" % OWNER)])
    _write(tmp_path / "user" / OWNER / "a.txt", "new")

    result = svc.restore_from_trash(OWNER, svc.list_trash(OWNER)[0]["batch_id"])

    assert result["restored"][0]["path"] == "user/%s/a (1).txt" % OWNER
    assert result["restored"][0]["renamed"] is True
    assert (tmp_path / "user" / OWNER / "a.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "user" / OWNER / "a (1).txt").read_text(
        encoding="utf-8") == "old"


def test_restore_recreates_a_deleted_parent_directory(tmp_path):
    _write(tmp_path / "user" / OWNER / "docs" / "a.txt", "body")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/docs/a.txt" % OWNER)])
    os.rmdir(tmp_path / "user" / OWNER / "docs")

    svc.restore_from_trash(OWNER, svc.list_trash(OWNER)[0]["batch_id"])

    assert (tmp_path / "user" / OWNER / "docs" / "a.txt").read_text(
        encoding="utf-8") == "body"


def test_restore_of_a_protected_path_is_refused_with_a_reason(tmp_path):
    _write(tmp_path / "memory" / "note.md", "m")
    svc = _service(tmp_path)
    result = svc.move_to_trash(OWNER, [_item(svc, "memory/note.md")])
    batch = result["batch_id"]

    restored = svc.restore_from_trash(OWNER, batch)

    assert restored["restored"] == []
    assert restored["failed"][0]["code"] == "agent_internal"
    # Still held, so nothing was destroyed by the refusal.
    assert len(svc.list_trash(OWNER)) == 1


def test_restore_of_an_unknown_batch_reports_it(tmp_path):
    result = _service(tmp_path).restore_from_trash(OWNER, "20260101T000000-deadbeef")

    assert result["restored"] == []
    assert result["failed"] == [{"index": None, "code": "batch_not_found"}]


def test_restore_batch_id_cannot_be_a_path(tmp_path):
    from common.safe_fs import UnsafePathError

    svc = _service(tmp_path)
    with pytest.raises(UnsafePathError):
        svc.restore_from_trash(OWNER, "../../user/u-bob/.trash/x")


# ----------------------------------------------------------------------
# purge_trash
# ----------------------------------------------------------------------
def test_purge_destroys_one_item_and_keeps_the_batch_alive(tmp_path):
    _write(tmp_path / "user" / OWNER / "a.txt", "a")
    _write(tmp_path / "user" / OWNER / "b.txt", "b")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/a.txt" % OWNER),
                              _item(svc, "user/%s/b.txt" % OWNER)])
    batch = svc.list_trash(OWNER)[0]["batch_id"]

    result = svc.purge_trash(OWNER, batch, [0])

    assert result["failed"] == []
    assert [e["rel"] for e in svc.list_trash(OWNER)] == ["user/%s/b.txt" % OWNER]


def test_purging_everything_removes_the_batch_directory(tmp_path):
    """Emptying a batch item by item must not leave bookkeeping behind."""
    _write(tmp_path / "user" / OWNER / "a.txt", "a")
    _write(tmp_path / "user" / OWNER / "b.txt", "b")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/a.txt" % OWNER),
                              _item(svc, "user/%s/b.txt" % OWNER)])
    batch = svc.list_trash(OWNER)[0]["batch_id"]

    svc.purge_trash(OWNER, batch, [0])
    svc.purge_trash(OWNER, batch, [1])

    assert not (tmp_path / "user" / OWNER / ".trash" / batch).exists()


def test_purge_whole_batch_is_idempotent(tmp_path):
    _write(tmp_path / "user" / OWNER / "a.txt", "a")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/a.txt" % OWNER)])
    batch = svc.list_trash(OWNER)[0]["batch_id"]

    assert svc.purge_trash(OWNER, batch)["purged"]
    second = svc.purge_trash(OWNER, batch)
    assert second["purged"] == []
    assert second["failed"] == [{"index": None, "code": "batch_not_found"}]


def test_purge_does_not_follow_a_link_out_of_the_bin(tmp_path):
    """The payload came from a user directory, so it may contain links."""
    target = _write(tmp_path / "user" / OWNER / "keep.txt", "keep")
    svc = _service(tmp_path)
    os.makedirs(tmp_path / "user" / OWNER / "d")
    try:
        os.symlink(str(target), str(tmp_path / "user" / OWNER / "d" / "lnk"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this platform")
    svc.move_to_trash(OWNER, [_item(svc, "user/%s/d" % OWNER)])
    batch = svc.list_trash(OWNER)[0]["batch_id"]

    result = svc.purge_trash(OWNER, batch)

    assert result["failed"] == []
    assert target.read_text(encoding="utf-8") == "keep"


# ----------------------------------------------------------------------
# cleanup_trash
# ----------------------------------------------------------------------
def test_retention_collects_stale_batches_only(tmp_path):
    _write(tmp_path / "user" / OWNER / "old.txt", "o")
    _write(tmp_path / "user" / OWNER / "fresh.txt", "f")
    svc = _service(tmp_path)
    old_batch = svc.move_to_trash(
        OWNER, [_item(svc, "user/%s/old.txt" % OWNER)])["batch_id"]
    fresh_batch = svc.move_to_trash(
        OWNER, [_item(svc, "user/%s/fresh.txt" % OWNER)])["batch_id"]

    stale = time.time() - (service_module.TRASH_RETENTION_SECONDS + 60)
    os.utime(tmp_path / "user" / OWNER / ".trash" / old_batch, (stale, stale))

    assert svc.cleanup_trash(OWNER) == 1
    remaining = svc.list_trash(OWNER)
    assert [e["batch_id"] for e in remaining] == [fresh_batch]


def test_retention_on_a_missing_bin_is_a_no_op(tmp_path):
    assert _service(tmp_path).cleanup_trash(OWNER) == 0


# ----------------------------------------------------------------------
# The bin is not user content
# ----------------------------------------------------------------------
def test_batch_meta_is_readable_json(tmp_path):
    _write(tmp_path / "user" / OWNER / "a.txt", "a")
    svc = _service(tmp_path)
    batch = svc.move_to_trash(OWNER, [_item(svc, "user/%s/a.txt" % OWNER)])["batch_id"]

    meta = json.loads(
        (tmp_path / "user" / OWNER / ".trash" / batch / "batch.json"
         ).read_text(encoding="utf-8"))

    assert meta["batch_id"] == batch
    assert meta["items"][0]["state"] == "trashed"
    assert meta["items"][0]["rel"] == "user/%s/a.txt" % OWNER


def test_the_bin_is_anchored_on_the_agent_not_the_served_root(tmp_path):
    """The served root is not the Agent's workspace in database mode.

    There this service is rooted at the tenant's shared root and an Agent's
    workspace is ``agents/<id>`` inside it. A bin placed at
    ``<served root>/user/<uid>/.trash`` would sit outside every ownership rule
    (``classify_agent_user_path`` sees "not in this Agent at all"), so a
    colleague could list it -- the bin has to travel with the Agent.
    """
    _write(tmp_path / "agents" / "alpha" / "user" / OWNER / "a.txt", "a")
    svc = _service(tmp_path)

    result = svc.move_to_trash(
        OWNER, [_item(svc, "agents/alpha/user/%s/a.txt" % OWNER)],
        agent_rel="agents/alpha")

    assert not (tmp_path / "user").exists(), "bin leaked to the served root"
    payload = (tmp_path / "agents" / "alpha" / "user" / OWNER / ".trash"
               / result["batch_id"] / "files" / "0")
    assert payload.read_text(encoding="utf-8") == "a"


def test_the_bin_is_hidden_below_an_agent_prefixed_root(tmp_path):
    """``show_hidden=1`` must not reveal ``agents/<id>/user/<uid>/.trash``.

    A depth-relative match passes the per-Agent-workspace case and fails exactly
    here, which is the real deployment shape.
    """
    _write(tmp_path / "agents" / "alpha" / "user" / OWNER / "a.txt", "a")
    svc = _service(tmp_path)
    svc.move_to_trash(OWNER, [_item(svc, "agents/alpha/user/%s/a.txt" % OWNER)],
                      agent_rel="agents/alpha")

    listing = svc.list_dir("agents/alpha/user/%s" % OWNER, show_hidden=True)

    assert {e["name"] for e in listing["entries"]} == set()


def test_a_trash_folder_deeper_in_the_users_own_tree_is_their_content(tmp_path):
    """Only the bin at ``user/<uid>/.trash`` is ours to hide."""
    _write(tmp_path / "agents" / "alpha" / "user" / OWNER / "docs" / ".trash"
           / "keep.txt", "k")
    svc = _service(tmp_path)

    listing = svc.list_dir("agents/alpha/user/%s/docs" % OWNER, show_hidden=True)

    assert {e["name"] for e in listing["entries"]} == {".trash"}


def test_restore_revalidates_the_destination_through_the_callers_guard(tmp_path):
    """Restore is another write, so a narrowed range must refuse it.

    ``dest_guard`` is the same seam the delete ran; here it models the range
    having shrunk to ``user/<uid>`` while the entry came from the Agent root.
    """
    _write(tmp_path / "agents" / "alpha" / "docs" / "a.txt", "a")
    svc = _service(tmp_path)
    batch = svc.move_to_trash(
        OWNER, [_item(svc, "agents/alpha/docs/a.txt")],
        agent_rel="agents/alpha")["batch_id"]

    narrowed = "agents/alpha/user/%s" % OWNER
    result = svc.restore_from_trash(
        OWNER, batch, agent_rel="agents/alpha",
        dest_guard=lambda rel: (None if rel.startswith(narrowed + "/")
                                else "outside_own_directory"))

    assert result["restored"] == []
    assert result["failed"] == [{"index": 0,
                                 "rel": "agents/alpha/docs/a.txt",
                                 "code": "outside_own_directory"}]
    # Refused, so still held rather than lost.
    assert len(svc.list_trash(OWNER, agent_rel="agents/alpha")) == 1
    assert not (tmp_path / "agents" / "alpha" / "docs" / "a.txt").exists()
