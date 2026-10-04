"""Upload and delete on the file panel, over the real Wire.

Change ``add-workspace-panel-upload-and-delete``. The unit tests
(``test_workspace_trash.py``) exercise the service primitives directly; what
only the real WSGI app can answer is whether the *routes* apply the writable
range -- the route policy, the tenant-from-resource derivation, the session
cookie and the handler's own scope all sit between the rule and the bytes.

The fixture is the product's own shape: Alice and Bob are two ``member``s of one
tenant sharing one Agent that lives inside the tenant's shared root (so the
console can address it as ``agents/<id>``), plus a second Agent owned privately
by Alice, whose whole directory is her writable range.

Refusals are asserted by their response ``code`` rather than by "it did not say
success": a bare failure assertion is also satisfied by a handler that refuses
everything.
"""

import json
import os

import pytest

from tests._helpers import WebAppHarness

AGENT = "shared-agent"
PRIVATE = "private-agent"
BOUNDARY = "----cowtest"


@pytest.fixture()
def web(tmp_path):
    harness = WebAppHarness(tmp_path)
    # An Agent workspace nested under the tenant shared root -- the layout the
    # console's tree/search walk from, and the one where the bin's anchoring is
    # easy to get wrong.
    harness.workspace = os.path.join(harness.shared_root, "agents", AGENT)
    harness.write_roster([
        {"id": AGENT, "name": "Shared", "workspace": harness.workspace},
    ])
    harness.add_agent(AGENT)
    harness.alice = harness.member("alice", ["member"])
    harness.bob = harness.member("bob", ["member"])
    # A second Agent owned privately by Alice: she is its only user, so the whole
    # directory is her writable range.
    harness.private_agent(harness.alice, PRIVATE)
    harness.private_ws = os.path.join(harness.shared_root, "agents", PRIVATE)
    for path in (os.path.join(harness.workspace, "memory"),
                 os.path.join(harness.private_ws, "memory")):
        os.makedirs(path, exist_ok=True)
    yield harness
    harness.close()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def multipart(fields, filename=None, content=b"payload"):
    """One multipart body, exactly as the panel's XHR sends it.

    The panel posts **one file per request** -- that is what makes a 200MB file
    and a 5000-file drop the same code path, and what lets the client report
    exact bytes instead of scaling one monolithic body.

    Returned as ``str`` because web.py's request helper re-encodes a ``bytes``
    body with ``.encode()``. Every test payload here is valid UTF-8, so the
    round-trip is byte-exact.
    """
    body = b""
    for name, value in fields:
        body += (
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
            % (BOUNDARY, name, value)
        ).encode("utf-8")
    if filename is not None:
        body += (
            "--%s\r\n"
            'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            % (BOUNDARY, filename)
        ).encode("utf-8")
        body += content
        body += b"\r\n"
    body += ("--%s--\r\n" % BOUNDARY).encode("utf-8")
    return body.decode("utf-8")


def upload(web, user, *, agent, target_dir, relative=None, filename="a.txt",
           content=b"payload", extra=()):
    fields = [("dir", target_dir)]
    if relative:
        fields.append(("relative_path", relative))
    fields.extend(extra)
    return web.post(
        "/api/workspace/upload?agent=" + agent,
        multipart(fields, filename=filename, content=content),
        token=web.login(user),
        headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})


def delete(web, user, targets, *, agent=AGENT):
    return web.post("/api/workspace/delete?agent=" + agent,
                    {"targets": targets}, token=web.login(user))


def trash(web, user, *, agent=AGENT):
    return web.get("/api/workspace/trash?agent=" + agent, token=web.login(user))


def restore(web, user, batch_id, *, agent=AGENT):
    return web.post("/api/workspace/trash/restore?agent=" + agent,
                    {"batch_id": batch_id}, token=web.login(user))


def purge(web, user, batch_id=None, *, agent=AGENT):
    body = {} if batch_id is None else {"batch_id": batch_id}
    return web.post("/api/workspace/trash/purge?agent=" + agent, body,
                    token=web.login(user))


def json_of(response):
    assert response.status.startswith("200"), response.status
    return json.loads(response.data.decode("utf-8"))


def own(web, user, *parts):
    return os.path.join(web.workspace, "user", user, *parts)


def seed(web, user, *parts, content=b"seed"):
    path = own(web, user, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def rel_of(web, user, *parts):
    return "/".join(("agents", AGENT, "user", user) + parts)


# ----------------------------------------------------------------------
# Upload
# ----------------------------------------------------------------------
def test_a_member_uploads_into_their_own_directory(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s" % (AGENT, web.alice),
                          relative="notes/today.txt", filename="today.txt"))

    assert body["status"] == "success", body
    assert body["path"] == rel_of(web, web.alice, "notes", "today.txt")
    assert body["renamed"] is False
    assert body["size"] == len(b"payload")
    with open(own(web, web.alice, "notes", "today.txt"), "rb") as handle:
        assert handle.read() == b"payload"


def test_a_drop_into_the_folder_shown_lands_directly_in_it(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s" % (AGENT, web.alice),
                          filename="loose.txt"))

    assert body["path"] == rel_of(web, web.alice, "loose.txt")


def test_a_dragged_folder_structure_is_recreated(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s" % (AGENT, web.alice),
                          relative="支持性文档/2024/合同/a.xlsx",
                          filename="a.xlsx"))

    assert body["status"] == "success", body
    assert os.path.isfile(
        own(web, web.alice, "支持性文档", "2024", "合同", "a.xlsx"))


def test_a_collision_is_renamed_never_overwritten(web):
    """The panel ships no undo for an eaten file, so it must not eat one."""
    existing = seed(web, web.alice, "报告.xlsx", content=b"original")

    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s" % (AGENT, web.alice),
                          relative="报告.xlsx", filename="报告.xlsx",
                          content=b"replacement"))

    assert body["status"] == "success", body
    assert body["renamed"] is True
    assert body["path"] == rel_of(web, web.alice, "报告 (1).xlsx")
    with open(existing, "rb") as handle:
        assert handle.read() == b"original"
    with open(own(web, web.alice, "报告 (1).xlsx"), "rb") as handle:
        assert handle.read() == b"replacement"


def test_a_member_cannot_upload_into_a_colleagues_directory(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s" % (AGENT, web.bob),
                          relative="planted.txt", filename="planted.txt"))

    assert body["status"] == "error"
    assert body["code"] == "outside_own_directory"
    assert not os.path.exists(own(web, web.bob, "planted.txt"))
    # ...and the refusal does not even confirm the directory exists.
    assert web.bob not in body.get("message", "")


def test_the_shared_root_is_not_a_landing_point(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/" + AGENT,
                          relative="planted.txt", filename="planted.txt"))

    assert body["status"] == "error"
    assert body["code"] == "outside_own_directory"
    assert not os.path.exists(os.path.join(web.workspace, "planted.txt"))


def test_a_member_cannot_upload_into_the_agents_own_directories(web):
    """``knowledge/`` is readable by the member and still not theirs to write."""
    os.makedirs(os.path.join(web.workspace, "knowledge"), exist_ok=True)

    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/knowledge" % AGENT,
                          relative="planted.md", filename="planted.md"))

    assert body["status"] == "error"
    assert body["code"] == "outside_own_directory"
    assert not os.path.exists(os.path.join(web.workspace, "knowledge", "planted.md"))


def test_an_escaping_relative_path_is_refused(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s" % (AGENT, web.alice),
                          relative="../../%s/planted.txt" % web.bob,
                          filename="planted.txt"))

    assert body["status"] == "error"
    assert body["code"] == "outside_own_directory"
    assert not os.path.exists(own(web, web.bob, "planted.txt"))


def test_the_bin_is_not_a_landing_point(web):
    body = json_of(upload(web, "alice", agent=AGENT,
                          target_dir="agents/%s/user/%s/.trash" % (AGENT, web.alice),
                          relative="x.txt", filename="x.txt"))

    assert body["status"] == "error"
    assert body["code"] == "trash_not_targetable"


def test_a_private_agents_owner_uploads_anywhere_in_its_workspace(web):
    body = json_of(upload(web, "alice", agent=PRIVATE,
                          target_dir="agents/" + PRIVATE,
                          relative="docs/mine.txt", filename="mine.txt",
                          content=b"mine"))

    assert body["status"] == "success", body
    with open(os.path.join(web.private_ws, "docs", "mine.txt"), "rb") as handle:
        assert handle.read() == b"mine"


def test_a_private_agents_owner_may_append_to_the_agents_own_directories(web):
    """Uploading is appending, so the delete-protection list does not apply.

    Adding a file to the Agent's ``memory/`` does not break the Agent; deleting
    ``memory/`` does. Constraining both would only stop the owner from filing
    anything into their own Agent.
    """
    body = json_of(upload(web, "alice", agent=PRIVATE,
                          target_dir="agents/%s/memory" % PRIVATE,
                          relative="note.md", filename="note.md",
                          content=b"n"))

    assert body["status"] == "success", body
    with open(os.path.join(web.private_ws, "memory", "note.md"), "rb") as handle:
        assert handle.read() == b"n"


def test_a_member_cannot_upload_into_the_bins_of_their_own_agent(web):
    """The bin is the one entry a write must never address."""
    body = json_of(upload(web, "alice", agent=PRIVATE,
                          target_dir="agents/%s/user/%s/.trash"
                                     % (PRIVATE, web.alice),
                          relative="x.txt", filename="x.txt"))

    assert body["status"] == "error"
    assert body["code"] == "trash_not_targetable"


def test_a_private_agent_is_invisible_to_a_colleague(web):
    response = upload(web, "bob", agent=PRIVATE,
                      target_dir="agents/" + PRIVATE,
                      relative="planted.txt", filename="planted.txt")

    assert not response.status.startswith("200"), response.data
    assert not os.path.exists(os.path.join(web.private_ws, "planted.txt"))


def test_an_upload_without_a_file_part_is_reported_as_such(web):
    response = web.post(
        "/api/workspace/upload?agent=" + AGENT,
        multipart([("dir", "agents/%s/user/%s" % (AGENT, web.alice))]),
        token=web.login("alice"),
        headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})

    body = json_of(response)
    assert body["status"] == "error"
    assert body["code"] == "no_file"


def test_a_file_over_the_servers_own_ceiling_is_refused_by_code(web, monkeypatch):
    """The panel must be able to name the reason, and the ceiling is the app's.

    A proxy's 413 never reaches here (the response carries no ``code`` for the
    panel to read), so this is the other half of the same user-visible
    condition: the request arrived, the handler counted the bytes, and it says
    which limit was hit. The oversize file must also leave nothing behind -- no
    half file, no ``.part`` sibling.

    The ceiling itself is lowered for this test rather than sending 200MB: the
    number is a module constant the handler reads at call time, and the real
    figure is measured over the wire by ``scripts/verify_workspace_upload_scale.py``.
    """
    from channel.web.fork.handlers import workspace as ws

    monkeypatch.setattr(ws, "WS_UPLOAD_MAX_BYTES", 4096)
    target_dir = "agents/%s/user/%s" % (AGENT, web.alice)
    response = upload(web, "alice", agent=AGENT, target_dir=target_dir,
                      relative="too-big.bin", filename="too-big.bin",
                      content=b"x" * 4097)

    body = json_of(response)
    assert body["status"] == "error", body
    assert body["code"] == "too_large", body
    assert "4096" in body["message"]

    landed = os.path.join(web.workspace, "user", web.alice)
    assert os.path.isdir(landed)
    assert [n for n in os.listdir(landed) if n.startswith("too-big")] == []

    # ... and the refusal is that one request, so the next file in the same drop
    # still lands (design D6: one file per request).
    neighbour = upload(web, "alice", agent=AGENT, target_dir=target_dir,
                       relative="fine.txt", filename="fine.txt", content=b"ok")
    assert json_of(neighbour)["status"] == "success"


def test_a_payload_that_does_not_arrive_whole_is_refused_by_its_own_code(web):
    """A truncated body must not be saved, and must be distinguishable.

    The panel's remedy differs from the oversize case -- upload this file again
    rather than split it -- so the code differs too.

    The condition is a body the transport declared complete and the parser
    accepted, whose bytes did not all arrive; it cannot be produced faithfully
    through ``app.request`` (web.py's request helper derives ``Content-Length``
    from the body it is given, so declared and actual always agree there). It is
    therefore driven against the guard itself, with a part stub whose stream
    promises more than it hands over -- which is exactly the input the guard
    exists to catch.
    """
    from agent.workspace.service import WorkspaceService
    from channel.web.fork.handlers.workspace import WS_UPLOAD_INCOMPLETE
    from channel.web.fork.handlers.workspace import WorkspaceUploadHandler

    class _DeclaredLongerThanSent:
        """The stream half of a part that promised more than it had."""

        @staticmethod
        def seek(*_args):
            return 0

        @staticmethod
        def tell():
            return 64

        @staticmethod
        def read(*_args):
            return b""

    class _Part:
        filename = "half.bin"
        value = b""
        file = _DeclaredLongerThanSent()

    svc = WorkspaceService(web.workspace)
    target = "user/%s/half.bin" % web.alice
    with pytest.raises(Exception) as caught:
        WorkspaceUploadHandler._stream_to(svc, target, _Part())

    assert getattr(caught.value, "code", None) == WS_UPLOAD_INCOMPLETE, caught.value
    landed = os.path.join(web.workspace, "user", web.alice)
    # Neither the final name nor the temp sibling survives the refusal.
    assert [n for n in os.listdir(landed) if n.startswith("half")] == []
    assert [n for n in os.listdir(landed) if n.endswith(".part")] == []


# ----------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------
def test_a_member_deletes_their_own_file_into_the_bin(web):
    target = seed(web, web.alice, "junk.txt", content=b"junk")

    body = json_of(delete(web, "alice", [rel_of(web, web.alice, "junk.txt")]))

    assert body["status"] == "success", body
    assert body["deleted"] == [rel_of(web, web.alice, "junk.txt")]
    assert body["batch_id"]
    assert body["reclaimed"] == 4
    assert not os.path.exists(target)
    # The file is in the bin, not gone.
    assert json_of(trash(web, "alice"))["entries"][0]["rel"] == \
        rel_of(web, web.alice, "junk.txt")


def test_deleting_a_folder_takes_its_contents_in_one_move(web):
    folder = own(web, web.alice, "proj")
    os.makedirs(os.path.join(folder, "sub", "deep"))
    with open(os.path.join(folder, "sub", "deep", "a.txt"), "wb") as handle:
        handle.write(b"a")

    body = json_of(delete(web, "alice", [rel_of(web, web.alice, "proj")]))

    assert body["status"] == "success", body
    assert not os.path.exists(folder)


def test_a_member_cannot_delete_a_colleagues_file(web):
    victim = seed(web, web.bob, "private.txt", content=b"bob's")

    body = json_of(delete(web, "alice", [rel_of(web, web.bob, "private.txt")]))

    assert body["status"] == "success"
    assert body["deleted"] == []
    assert body["failed"] == [{"rel": rel_of(web, web.bob, "private.txt"),
                               "code": "outside_own_directory"}]
    assert os.path.exists(victim)


def test_a_member_cannot_delete_the_agents_own_entries(web):
    agent_md = os.path.join(web.workspace, "AGENT.md")
    with open(agent_md, "wb") as handle:
        handle.write(b"persona")

    body = json_of(delete(web, "alice", [
        "agents/%s/AGENT.md" % AGENT, "agents/%s/memory" % AGENT]))

    assert body["status"] == "success"
    assert body["deleted"] == []
    assert [f["code"] for f in body["failed"]] == [
        "outside_own_directory", "outside_own_directory"]
    assert os.path.exists(agent_md)
    assert os.path.isdir(os.path.join(web.workspace, "memory"))


def test_a_private_agents_owner_cannot_delete_the_agents_own_entries(web):
    agent_md = os.path.join(web.private_ws, "AGENT.md")
    with open(agent_md, "wb") as handle:
        handle.write(b"persona")

    body = json_of(delete(web, "alice", [
        "agents/%s/AGENT.md" % PRIVATE, "agents/%s/memory" % PRIVATE],
        agent=PRIVATE))

    assert body["status"] == "success"
    assert body["deleted"] == []
    assert [f["code"] for f in body["failed"]] == ["agent_internal", "agent_internal"]
    assert os.path.exists(agent_md)


def test_a_private_agents_owner_deletes_their_own_ordinary_files(web):
    target = os.path.join(web.private_ws, "notes.md")
    with open(target, "wb") as handle:
        handle.write(b"n")

    body = json_of(delete(web, "alice", ["agents/%s/notes.md" % PRIVATE],
                          agent=PRIVATE))

    assert body["status"] == "success", body
    assert body["deleted"] == ["agents/%s/notes.md" % PRIVATE]
    assert not os.path.exists(target)


def test_the_callers_own_directory_is_not_a_delete_target(web):
    """Deleting it would carry the bin away with it, making the delete final."""
    os.makedirs(own(web, web.alice), exist_ok=True)

    body = json_of(delete(web, "alice", [rel_of(web, web.alice)]))

    assert body["status"] == "success"
    assert body["deleted"] == []
    assert body["failed"] == [{"rel": rel_of(web, web.alice),
                               "code": "own_directory_root"}]
    assert os.path.isdir(own(web, web.alice))


def test_the_user_container_is_not_a_delete_target(web):
    body = json_of(delete(web, "alice", ["agents/%s/user" % AGENT]))

    assert body["failed"] == [{"rel": "agents/%s/user" % AGENT,
                               "code": "outside_own_directory"}]


def test_the_bin_is_not_a_delete_target(web):
    """Emptying the bin is ``purge``, which names a batch; ``delete`` must not
    reach the bin itself either -- a delete that could carry the bin away would
    make every other delete final."""
    seed(web, web.alice, "gone.txt", content=b"g")
    batch = json_of(delete(web, "alice", [rel_of(web, web.alice, "gone.txt")]))[
        "batch_id"]

    body = json_of(delete(web, "alice", [
        "agents/%s/user/%s/.trash" % (AGENT, web.alice)]))

    assert body["deleted"] == []
    assert body["failed"] == [{
        "rel": "agents/%s/user/%s/.trash" % (AGENT, web.alice),
        "code": "trash_not_targetable"}]
    # The batch survived, which is what matters: a delete that could carry the
    # bin away would make every other delete final.
    assert [e["batch_id"] for e in json_of(trash(web, "alice"))["entries"]] == [batch]


def test_a_protected_name_inside_the_users_own_directory_is_ordinary_content(web):
    """The protected set matches the Agent's root only, never a name at depth."""
    folder = own(web, web.alice, "memory")
    os.makedirs(folder)
    with open(os.path.join(folder, "notes.md"), "wb") as handle:
        handle.write(b"n")

    body = json_of(delete(web, "alice", [rel_of(web, web.alice, "memory")]))

    assert body["deleted"] == [rel_of(web, web.alice, "memory")]
    assert not os.path.exists(folder)


def test_a_missing_target_fails_alone_without_undoing_the_rest(web):
    good = seed(web, web.alice, "here.txt", content=b"h")

    body = json_of(delete(web, "alice", [
        rel_of(web, web.alice, "here.txt"),
        rel_of(web, web.alice, "gone.txt")]))

    assert body["deleted"] == [rel_of(web, web.alice, "here.txt")]
    assert body["failed"] == [{"rel": rel_of(web, web.alice, "gone.txt"),
                               "code": "not_found"}]
    assert not os.path.exists(good)


def test_deleting_the_same_target_twice_moves_it_once(web):
    seed(web, web.alice, "once.txt", content=b"o")

    body = json_of(delete(web, "alice", [rel_of(web, web.alice, "once.txt")] * 2))

    assert body["deleted"] == [rel_of(web, web.alice, "once.txt")]
    assert len(json_of(trash(web, "alice"))["entries"]) == 1


def test_delete_without_targets_is_refused(web):
    body = json_of(web.post("/api/workspace/delete?agent=" + AGENT, {},
                            token=web.login("alice")))

    assert body["status"] == "error"
    assert body["code"] == "no_targets"


def test_delete_refuses_a_body_beyond_the_item_cap(web):
    body = json_of(delete(web, "alice", ["x%d" % n for n in range(2001)]))

    assert body["status"] == "error"
    assert body["code"] == "too_many_targets"


# ----------------------------------------------------------------------
# The bin: list, restore, purge
# ----------------------------------------------------------------------
def test_the_bin_lists_what_a_delete_put_there(web):
    seed(web, web.alice, "a.txt", content=b"aaa")
    delete(web, "alice", [rel_of(web, web.alice, "a.txt")])

    body = json_of(trash(web, "alice"))

    assert body["status"] == "success", body
    assert body["retention_days"] == 30
    assert body["total_size"] == 3
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["rel"] == rel_of(web, web.alice, "a.txt")
    assert entry["name"] == "a.txt"
    assert entry["size"] == 3
    assert entry["kind"] == "file"
    assert entry["batch_id"]
    # The bin never reports a host path for its payload.
    assert web.workspace.replace("\\", "/") not in json.dumps(body)


def test_the_bin_sits_inside_the_callers_own_directory(web):
    """It inherits the ownership rule instead of needing a second one.

    A bin at ``<tenant shared root>/user/<uid>/.trash`` would be inside no
    Agent at all, so ``_db_path_visible`` would treat it as ordinary tenant
    content and a colleague could list it.
    """
    seed(web, web.alice, "a.txt", content=b"a")
    delete(web, "alice", [rel_of(web, web.alice, "a.txt")])

    assert os.path.isdir(own(web, web.alice, ".trash"))
    assert not os.path.exists(os.path.join(web.shared_root, "user"))


def test_the_bin_never_appears_in_the_listing_even_when_hidden_files_are_shown(web):
    """``show_hidden`` reveals dotfiles; the bin must not be one of them."""
    seed(web, web.alice, "a.txt", content=b"a")
    delete(web, "alice", [rel_of(web, web.alice, "a.txt")])

    response = web.get(
        "/api/workspace/tree?agent=%s&path=agents/%s/user/%s&show_hidden=1"
        % (AGENT, AGENT, web.alice), token=web.login("alice"))
    names = {entry["name"] for entry in json_of(response)["entries"]}

    assert ".trash" not in names, names


def test_the_bin_never_appears_in_search(web):
    seed(web, web.alice, "findme.txt", content=b"a")
    delete(web, "alice", [rel_of(web, web.alice, "findme.txt")])

    response = web.get(
        "/api/workspace/search?agent=%s&q=findme" % AGENT, token=web.login("alice"))

    assert json_of(response)["results"] == []


def test_the_bin_is_the_callers_own(web):
    seed(web, web.bob, "bobs.txt", content=b"b")
    bobs_batch = json_of(delete(web, "bob", [rel_of(web, web.bob, "bobs.txt")]))[
        "batch_id"]
    assert bobs_batch

    assert json_of(trash(web, "alice"))["entries"] == []

    refused = json_of(purge(web, "alice", bobs_batch))
    assert refused["purged"] == []
    assert refused["failed"] == [{"index": None, "code": "batch_not_found"}]
    # Bob's entry is exactly where it was.
    assert len(json_of(trash(web, "bob"))["entries"]) == 1


def test_restore_puts_an_entry_back_where_it_was(web):
    seed(web, web.alice, "docs", "a.txt", content=b"body")
    rel = rel_of(web, web.alice, "docs", "a.txt")
    batch = json_of(delete(web, "alice", [rel]))["batch_id"]

    body = json_of(restore(web, "alice", batch))

    assert body["status"] == "success", body
    assert body["failed"] == []
    assert body["restored"] == [{"index": 0, "rel": rel, "path": rel,
                                 "renamed": False}]
    with open(own(web, web.alice, "docs", "a.txt"), "rb") as handle:
        assert handle.read() == b"body"
    assert json_of(trash(web, "alice"))["entries"] == []


def test_restore_rebuilds_a_deleted_directory_tree(web):
    seed(web, web.alice, "proj", "sub", "a.txt", content=b"a")
    rel = rel_of(web, web.alice, "proj")
    batch = json_of(delete(web, "alice", [rel]))["batch_id"]

    body = json_of(restore(web, "alice", batch))

    assert body["restored"][0]["path"] == rel
    with open(own(web, web.alice, "proj", "sub", "a.txt"), "rb") as handle:
        assert handle.read() == b"a"


def test_restore_renames_rather_than_clobbering(web):
    seed(web, web.alice, "a.txt", content=b"old")
    rel = rel_of(web, web.alice, "a.txt")
    batch = json_of(delete(web, "alice", [rel]))["batch_id"]
    seed(web, web.alice, "a.txt", content=b"new")

    body = json_of(restore(web, "alice", batch))

    assert body["restored"][0]["renamed"] is True
    assert body["restored"][0]["path"] == rel_of(web, web.alice, "a (1).txt")
    with open(own(web, web.alice, "a.txt"), "rb") as handle:
        assert handle.read() == b"new"
    with open(own(web, web.alice, "a (1).txt"), "rb") as handle:
        assert handle.read() == b"old"


def test_purge_destroys_one_entry_for_good(web):
    seed(web, web.alice, "a.txt", content=b"a")
    batch = json_of(delete(web, "alice", [rel_of(web, web.alice, "a.txt")]))[
        "batch_id"]

    body = json_of(purge(web, "alice", batch))

    assert body["status"] == "success", body
    assert len(body["purged"]) == 1
    assert json_of(trash(web, "alice"))["entries"] == []
    assert not os.path.exists(own(web, web.alice, ".trash", batch))


def test_purge_without_a_batch_empties_the_callers_bin(web):
    for name in ("a.txt", "b.txt"):
        seed(web, web.alice, name, content=b"x")
        delete(web, "alice", [rel_of(web, web.alice, name)])

    body = json_of(purge(web, "alice"))

    assert len(body["purged"]) == 2
    assert json_of(trash(web, "alice"))["entries"] == []


def test_purging_without_a_batch_leaves_a_colleagues_bin_alone(web):
    seed(web, web.bob, "b.txt", content=b"b")
    delete(web, "bob", [rel_of(web, web.bob, "b.txt")])

    json_of(purge(web, "alice"))

    assert len(json_of(trash(web, "bob"))["entries"]) == 1


def test_a_batch_id_cannot_address_a_path_outside_the_bin(web):
    body = json_of(purge(web, "alice", "../../../%s/.trash/x" % web.bob))

    assert body["status"] == "error"
    assert body["code"] == "unsafe_path"


def test_restore_of_an_unknown_batch_says_so(web):
    body = json_of(restore(web, "alice", "20260101T000000-deadbeef"))

    assert body["status"] == "success"
    assert body["restored"] == []
    assert body["failed"] == [{"index": None, "code": "batch_not_found"}]


def test_restore_refuses_a_destination_the_agent_no_longer_allows(web):
    """A private Agent made shared narrows the range; a restore may not bypass it.

    The item was deleted while Alice owned the whole Agent, so its original path
    is at the Agent root -- outside ``user/<uid>``, which is all a shared Agent's
    member may write.
    """
    os.makedirs(os.path.join(web.private_ws, "docs"), exist_ok=True)
    with open(os.path.join(web.private_ws, "docs", "a.txt"), "wb") as handle:
        handle.write(b"a")
    batch = json_of(delete(web, "alice", ["agents/%s/docs/a.txt" % PRIVATE],
                           agent=PRIVATE))["batch_id"]

    # Alice is still the private owner, so narrow the range the way a
    # visibility flip would: the same items, listed through a shared-like scope.
    from agent.workspace.service import WorkspaceService
    svc = WorkspaceService(web.shared_root)
    guard = lambda rel: (
        None if rel.startswith("agents/%s/user/%s/" % (PRIVATE, web.alice))
        else "outside_own_directory")
    result = svc.restore_from_trash(web.alice, batch, agent_rel="agents/" + PRIVATE,
                                    dest_guard=guard)

    assert result["restored"] == []
    assert result["failed"][0]["code"] == "outside_own_directory"
    assert len(svc.list_trash(web.alice, agent_rel="agents/" + PRIVATE)) == 1


def test_a_private_agents_owner_has_a_bin_of_their_own(web):
    target = os.path.join(web.private_ws, "notes.md")
    with open(target, "wb") as handle:
        handle.write(b"n")
    batch = json_of(delete(web, "alice", ["agents/%s/notes.md" % PRIVATE],
                           agent=PRIVATE))["batch_id"]

    listing = json_of(trash(web, "alice", agent=PRIVATE))
    assert [e["rel"] for e in listing["entries"]] == [
        "agents/%s/notes.md" % PRIVATE]

    body = json_of(restore(web, "alice", batch, agent=PRIVATE))
    assert body["restored"][0]["path"] == "agents/%s/notes.md" % PRIVATE
    assert os.path.exists(target)


# ----------------------------------------------------------------------
# The chat panel's existing contracts are untouched
# ----------------------------------------------------------------------
def test_the_read_routes_still_serve_the_users_own_files(web):
    seed(web, web.alice, "uploads", "a.txt", content=b"alice-private")

    tree = web.get(
        "/api/workspace/tree?agent=%s&path=agents/%s/user/%s"
        % (AGENT, AGENT, web.alice), token=web.login("alice"))
    assert json_of(tree)["entries"][0]["name"] == "uploads"

    read = web.get(
        "/api/workspace/read?agent=%s&path=%s"
        % (AGENT, rel_of(web, web.alice, "uploads", "a.txt")),
        token=web.login("alice"))
    assert json_of(read)["content"] == "alice-private"
