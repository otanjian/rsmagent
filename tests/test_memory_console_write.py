# encoding:utf-8
"""Task 5.1 (second half): edit / delete / clear on the Agent-domain memory.

The memory page listed and read two scopes (task 5.1's first half); this is the
same page's write side. Four properties are worth more than the CRUD itself, and
each is asserted here against the real ``build_web_app()`` application:

1. **Authorization precedes the mutation.** A caller who may not *read* a target
   must not be able to write it, and the refusal must happen before anything is
   touched — so the tests assert the file is byte-identical afterwards, not just
   that the response was a refusal. A filter applied after the write would pass
   a status-only test.
2. **A write is confined to memory only one scope reads.** An Agent memory root
   is the tenant's *shared* root in database mode, so the same bytes serve "my
   private Agent's memory" and "the tenant's shared Agent memory". A read may
   tolerate that; a write may not, and the test that measures both directions
   (``test_a_member_write_cannot_be_observed_on_the_shared_scope``) is the one
   that would have caught the escalation this guard closes.
3. **One write implementation, two domains.** The flow here is the delivered
   ``PersonalMemoryService`` flow (revision condition, atomic write, publish
   intent, tombstone journal, pending retry) inherited by ``MemoryService`` for
   the Agent root. So the revision conflicts and the read-only categories are
   asserted here exactly as the personal console asserts them — if this surface
   had grown a second implementation, these are the assertions that would drift.
4. **A deletion is not "gone" until it is gone.** ``MemoryManager.sync`` never
   prunes chunks whose file was removed, so a delete that reports success while
   its purge failed would keep serving the deleted body through search. The
   tombstone has to reach the retrieval filter — which is a different entry
   point, and therefore asserted directly (``test_the_deleted_body_...``).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

#: An Agent memory root is the tenant root in database mode
#: (``channel/web/tenant_workspace.py``), so a private Agent's memory and the
#: shared Agent's memory are the same files. That is why the write verbs below
#: are exercised as the *administration* on the shared Agent: the guard admits a
#: write only for a caller qualified on every scope reaching the root.
MAIN = "MEMORY.md"


def _status(response) -> int:
    return int(response.status.split()[0])


def _body(response) -> dict:
    return json.loads(response.data.decode("utf-8"))


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _boom(*_args, **_kwargs):
    """Stand-in for a write that a refusal must never reach."""
    raise AssertionError("memory was written before the refusal")


class _World:
    """One tenant: a shared Agent, two members with private Agents, an admin."""

    def __init__(self, harness):
        self.h = harness
        self.tenant_id = harness.tenant_id
        self.root = harness.shared_root
        harness.add_agent("shared-agent", "alice-agent", "bob-agent")
        self.alice = harness.member("alice", ["member"])
        self.bob = harness.member("bob", ["member"])
        self.admin = harness.member("admin", ["tenant_admin"])
        self.alice_token = harness.login("alice")
        self.bob_token = harness.login("bob")
        self.admin_token = harness.login("admin")
        self.bind_private("alice-agent", self.alice)
        self.bind_private("bob-agent", self.bob)

    def bind_private(self, agent_id: str, owner_user_id: str) -> None:
        self.h.service.bind_agent(tenant_id=self.tenant_id, agent_id=agent_id,
                                  private_owner_user_id=owner_user_id)

    def entry(self, name: str = MAIN) -> str:
        return os.path.join(self.root, name)

    def save(self, token, *, agent_id, content, filename=MAIN,
             category="memory", revision=None, scope=None):
        body = {"filename": filename, "category": category,
                "agent_id": agent_id, "content": content}
        if revision is not None:
            body["revision"] = revision
        if scope is not None:
            body["scope"] = scope
        return self.h.post("/api/memory/save", body, token=token)

    def delete(self, token, *, agent_id, filename=MAIN, category="memory",
               revision=None):
        body = {"filename": filename, "category": category,
                "agent_id": agent_id}
        if revision is not None:
            body["revision"] = revision
        return self.h.post("/api/memory/delete", body, token=token)

    def clear(self, token, *, agent_id, category="memory", revision=None):
        body = {"category": category, "agent_id": agent_id}
        if revision is not None:
            body["revision"] = revision
        return self.h.post("/api/memory/clear", body, token=token)

    def content(self, token, *, agent_id, filename=MAIN, category="memory"):
        return self.h.get(
            "/api/memory/content?filename=%s&category=%s&agent_id=%s"
            % (filename, category, agent_id), token=token)

    def list(self, token, *, agent_id, category="memory"):
        return self.h.get("/api/memory?category=%s&agent_id=%s"
                          % (category, agent_id), token=token)

    def revision(self, token, *, agent_id, filename=MAIN, category="memory"):
        """The revision the page would have read before offering an edit.

        Seeding a file behind the API's back is an out-of-band change: the
        version condition refuses an edit that never read it, so a test that
        wants to *edit* has to start where the page starts.
        """
        response = self.content(token, agent_id=agent_id, filename=filename,
                                category=category)
        assert _status(response) == 200, response.data
        return _body(response)["revision"]


@pytest.fixture
def world(web_app):
    return _World(web_app("memory-console-write"))


# --- the positive path ------------------------------------------------------

def test_the_administration_edits_the_shared_agents_memory(world):
    """The shared Agent is a tenant resource: its administration may edit it."""
    path = _write(world.entry(), "SHARED ORIGINAL\n")
    revision = world.revision(world.admin_token, agent_id="shared-agent")

    saved = world.save(world.admin_token, agent_id="shared-agent",
                       content="ADMIN EDIT\n", revision=revision)
    assert _status(saved) == 200, saved.data
    body = _body(saved)
    assert body["status"] == "success"
    assert body["scope"] == "shared"
    assert body["agent_id"] == "shared-agent"
    assert body["action"] == "save"
    assert body["index_state"] in ("ok", "pending")
    assert _read(path) == "ADMIN EDIT\n"

    # Read back through the same surface the page uses: list and content have to
    # agree with what the write reported, or the page would show a stale body.
    back = world.content(world.admin_token, agent_id="shared-agent")
    assert _status(back) == 200, back.data
    assert _body(back)["content"] == "ADMIN EDIT\n"
    assert _body(back)["revision"] == body["result"]["revision"]


def test_a_new_entry_is_created_by_save(world):
    """Saving an entry that does not exist yet creates it (no separate verb)."""
    saved = world.save(world.admin_token, agent_id="shared-agent",
                       filename="notes.md", content="FRESH NOTE\n")
    assert _status(saved) == 200, saved.data
    assert _read(world.entry("memory/notes.md")) == "FRESH NOTE\n"


# --- the member keeps the read, loses the write -----------------------------

def test_an_owner_may_not_write_their_private_agents_memory(world):
    """The member's own Agent is readable by them, and not writable.

    "My private Agent's memory" is the tenant's shared memory in database mode,
    so a write through the private scope is a write to a resource whose readers
    the caller does not administer. The read is untouched: the refusal is about
    the verb, not about the target's visibility.
    """
    path = _write(world.entry(), "SHARED BY BYTES\n")

    response = world.save(world.alice_token, agent_id="alice-agent",
                          content="ALICE WAS HERE\n")
    assert _status(response) == 403, response.data
    assert _body(response)["code"] == "not_authorized"
    assert _read(path) == "SHARED BY BYTES\n"

    listed = world.list(world.alice_token, agent_id="alice-agent")
    assert _status(listed) == 200, listed.data
    assert [row["filename"] for row in _body(listed)["list"]] == [MAIN]


def test_a_member_write_cannot_be_observed_on_the_shared_scope(world):
    """The escalation this guard closes, asserted in both directions.

    A "save" that silently landed on the shared Agent's memory and a "delete"
    that removed its file are different failures, and a test of one would not
    catch the other. Measured with the per-target ownership rule alone: the
    shared scope read the member's text, and the shared file was gone after the
    member's delete. The same assertion is what would fail again if the root
    guard were relaxed without giving a private Agent its own memory root.
    """
    path = _write(world.entry(), "TENANT BODY\n")

    for call in (lambda: world.save(world.alice_token, agent_id="alice-agent",
                                    content="MEMBER INJECTION\n"),
                 lambda: world.delete(world.alice_token, agent_id="alice-agent"),
                 lambda: world.clear(world.alice_token, agent_id="alice-agent")):
        response = call()
        assert _status(response) == 403, response.data
        assert _body(response)["code"] == "not_authorized"

    assert os.path.exists(path), "the member's delete removed the shared file"
    assert _read(path) == "TENANT BODY\n", "the member's save reached shared memory"
    shared = world.content(world.admin_token, agent_id="shared-agent")
    assert _status(shared) == 200, shared.data
    assert _body(shared)["content"] == "TENANT BODY\n"


def test_the_page_is_not_offered_a_verb_the_write_would_refuse(world):
    """The refusal has to be visible *before* the click, not only after it.

    ``actions`` is what the console page renders its edit/delete buttons from,
    so a member's Agent page must report the verbs unavailable. The same payload
    for the administrator must report them available — that is what keeps this
    from passing for the trivial reason that ``actions`` is always false.
    """
    _write(world.entry(), "BODY\n")

    as_member = _body(world.content(world.alice_token, agent_id="alice-agent"))
    assert as_member["actions"] == {"edit": False, "delete": False}
    rows = _body(world.list(world.alice_token, agent_id="alice-agent"))["list"]
    assert rows and all(row["actions"] == {"edit": False, "delete": False}
                        for row in rows)

    as_admin = _body(world.content(world.admin_token, agent_id="shared-agent"))
    assert as_admin["actions"] == {"edit": True, "delete": True}


# --- authorization precedes the mutation ------------------------------------

def test_another_member_cannot_write_a_private_agent(world):
    """Refused *and* untouched: the check cannot be an after-the-fact filter."""
    path = _write(world.entry(), "ALICE ONLY\n")

    response = world.save(world.bob_token, agent_id="alice-agent",
                          content="BOB WAS HERE\n")
    assert _status(response) == 403, response.data
    assert _body(response)["code"] == "not_owner"
    assert _read(path) == "ALICE ONLY\n"


def test_the_administrator_cannot_write_another_members_private_agent(world):
    """Administration is not ownership: a private Agent stays private.

    The ownership rule is checked before the administrator exception, so this
    stays ``not_owner`` even though the caller holds the qualification the root
    guard would accept.
    """
    path = _write(world.entry(), "ALICE ONLY\n")

    response = world.save(world.admin_token, agent_id="alice-agent",
                          content="ADMIN WAS HERE\n")
    assert _status(response) == 403, response.data
    assert _body(response)["code"] == "not_owner"
    assert _read(path) == "ALICE ONLY\n"


def test_a_member_cannot_write_the_tenant_shared_agent(world):
    """The reverse direction: the shared Agent needs administration."""
    path = _write(world.entry(), "TENANT OWNED\n")

    response = world.save(world.alice_token, agent_id="shared-agent",
                          content="MEMBER WAS HERE\n")
    assert _status(response) == 403, response.data
    assert _body(response)["code"] == "not_authorized"
    assert _read(path) == "TENANT OWNED\n"


def test_every_write_verb_is_refused_for_a_foreign_target(world):
    """delete and clear are not a way around the save-time check.

    Asserted together with the untouched file, because "the verb was refused"
    and "the entry still exists" are two different claims and only the pair
    means the write did not happen.
    """
    path = _write(world.entry(), "ALICE ONLY\n")

    for call in (lambda: world.delete(world.bob_token, agent_id="alice-agent"),
                 lambda: world.clear(world.bob_token, agent_id="alice-agent")):
        response = call()
        assert _status(response) == 403, response.data
        assert _body(response)["code"] == "not_owner"
        assert _read(path) == "ALICE ONLY\n"


def test_personal_writes_share_the_formal_endpoint_and_require_a_revision(world):
    seed = world.h.post('/api/memory/personal',
                        {'action': 'save', 'id': MAIN, 'content': 'ORIGINAL'},
                        token=world.alice_token)
    revision = _body(seed)['revision']
    missing = world.h.post('/api/memory/save',
        {'scope': 'personal', 'filename': MAIN, 'content': 'EDIT'}, token=world.alice_token)
    assert _status(missing) == 409
    assert _body(missing)['code'] == 'revision_required'
    changed = world.h.post('/api/memory/save',
        {'scope': 'personal', 'filename': MAIN, 'content': 'EDIT', 'revision': revision},
        token=world.alice_token)
    assert _body(changed)['status'] == 'success', changed.data
    read = world.h.get('/api/memory/personal/content?id=MEMORY.md', token=world.alice_token)
    assert _body(read)['content'] == 'EDIT'


def test_legacy_explicit_all_personal_clear_requires_collection_revision(world):
    world.h.post('/api/memory/personal',
        {'action': 'save', 'id': MAIN, 'content': 'KEEP'}, token=world.alice_token)
    missing = world.h.post('/api/memory/personal',
        {'action': 'clear', 'clear_scope': 'all_personal'}, token=world.alice_token)
    assert _status(missing) == 409
    assert _body(missing)['code'] == 'revision_required'
    read = world.h.get('/api/memory/personal/content?id=MEMORY.md', token=world.alice_token)
    assert _body(read)['content'] == 'KEEP'


# --- the version condition --------------------------------------------------

def test_a_stale_revision_loses_instead_of_overwriting(world):
    """Two pages, one winner: the second save is refused, not merged."""
    _write(world.entry(), "FIRST\n")
    first = world.save(world.admin_token, agent_id="shared-agent",
                       content="SECOND\n",
                       revision=world.revision(world.admin_token,
                                               agent_id="shared-agent"))
    assert _status(first) == 200, first.data
    stale = _body(first)["result"]["revision"]
    # Make the revision genuinely stale: someone else commits after it.
    second = world.save(world.admin_token, agent_id="shared-agent",
                        content="THIRD\n", revision=stale)
    assert _status(second) == 200, second.data

    response = world.save(world.admin_token, agent_id="shared-agent",
                          content="LOSER\n", revision=stale)
    assert _status(response) == 409, response.data
    assert _body(response)["code"] == "stale_revision"
    assert _read(world.entry()) == "THIRD\n"


def test_an_edit_without_the_revision_it_read_is_refused(world):
    """An existing entry may not be overwritten blind."""
    _write(world.entry(), "EXISTING\n")

    response = world.save(world.admin_token, agent_id="shared-agent",
                          content="BLIND\n")
    assert _status(response) == 409, response.data
    assert _body(response)["code"] == "revision_required"
    assert _read(world.entry()) == "EXISTING\n"


def test_a_delete_with_a_stale_revision_is_refused(world):
    path = _write(world.entry(), "FIRST\n")
    saved = world.save(world.admin_token, agent_id="shared-agent",
                       content="SECOND\n",
                       revision=world.revision(world.admin_token,
                                               agent_id="shared-agent"))
    stale = _body(saved)["result"]["revision"]
    world.save(world.admin_token, agent_id="shared-agent", content="THIRD\n",
               revision=stale)

    response = world.delete(world.admin_token, agent_id="shared-agent",
                            revision=stale)
    assert _status(response) == 409, response.data
    assert _read(path) == "THIRD\n"


# --- delete and clear -------------------------------------------------------

def test_delete_removes_the_entry_from_disk_and_from_the_list(world):
    _write(world.entry("memory/notes.md"), "NOTE\n")

    response = world.delete(world.admin_token, agent_id="shared-agent",
                            filename="notes.md")
    assert _status(response) == 200, response.data
    assert not os.path.exists(world.entry("memory/notes.md"))

    listed = world.list(world.admin_token, agent_id="shared-agent")
    assert _status(listed) == 200, listed.data
    assert [row["filename"] for row in _body(listed)["list"]] == []


def test_clear_empties_the_memory_category_but_not_the_agents_own_logs(world):
    """The Agent's dream/evolution diaries are the Agent's, not the caller's.

    ``clear`` is scoped to the category the page is showing: wiping a night's
    dream diary is not what "clear my memory entries" means, and the file is not
    user-authored.
    """
    _write(world.entry(MAIN), "MAIN\n")
    _write(world.entry("memory/2026-01-01.md"), "DAILY\n")
    dream = _write(world.entry("memory/dreams/2026-01-01.md"), "DREAM\n")

    response = world.clear(world.admin_token, agent_id="shared-agent")
    assert _status(response) == 200, response.data
    body = _body(response)
    assert body["status"] == "success"
    assert body["result"]["removed"] == 2, body

    assert not os.path.exists(world.entry(MAIN))
    assert not os.path.exists(world.entry("memory/2026-01-01.md"))
    assert _read(dream) == "DREAM\n", "the Agent's own diary is untouched"


# --- the categories the Agent writes itself ---------------------------------

def test_the_agents_own_categories_are_read_only_for_both_roles(world):
    """Not a role rule: a manual edit would be overwritten by consolidation.

    The category is checked before the caller's range, so both roles are refused
    for the same reason — the verb is refused on the category, whoever asks.
    """
    dream = _write(world.entry("memory/dreams/2026-01-01.md"), "DREAM\n")

    for token, agent in ((world.alice_token, "alice-agent"),
                         (world.admin_token, "shared-agent")):
        saved = world.save(token, agent_id=agent, category="dream",
                           filename="2026-01-01.md", content="EDIT\n")
        assert _status(saved) == 403, saved.data
        assert _body(saved)["code"] == "read_only_category"

        deleted = world.delete(token, agent_id=agent, category="dream",
                               filename="2026-01-01.md")
        assert _status(deleted) == 403, deleted.data
        assert _read(dream) == "DREAM\n"

        cleared = world.clear(token, agent_id=agent, category="dream")
        assert _status(cleared) == 403, cleared.data
        assert _read(dream) == "DREAM\n"


# --- the tombstone has to reach retrieval -----------------------------------

def _manager(workspace: str):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    config = MemoryConfig(workspace_root=workspace)
    config.sync_on_search = False
    manager = MemoryManager(config)
    manager.embedding_provider = None
    manager._dirty = False
    return manager


def _one_hit(manager, label: str):
    """Make the index answer with exactly one row for ``label``."""
    from agent.memory.storage import SearchResult

    manager.storage.search_keyword = lambda **_kwargs: [SearchResult(
        path=label, start_line=1, end_line=2, score=1.0,
        snippet="DELETED-SECRET", source="memory")]


def test_a_deleted_body_is_not_returned_while_its_purge_is_pending(
        world, monkeypatch):
    """The failure this whole journal exists for.

    ``MemoryManager.sync`` replaces a *changed* file's chunks and never sweeps
    chunks whose file is gone, so a delete whose index purge failed would keep
    the deleted body retrievable. The label therefore stays masked at the
    retrieval entry point until a retry succeeds. This is asserted through
    ``MemoryManager.search`` — the actual entry point — rather than by reading
    the journal, because a journal nobody consults masks nothing.
    """
    from agent.memory import personal as personal_mod

    _write(world.entry("memory/notes.md"), "DELETED-SECRET\n")
    manager = _manager(world.root)
    _one_hit(manager, "memory/notes.md")

    # Control: without any tombstone the row is served, so the assertion below
    # cannot pass for the trivial reason that search returns nothing here.
    control = asyncio.run(manager.search("secret", include_shared=True))
    assert [r.path for r in control] == ["memory/notes.md"], "control broken"

    def _purge_always_fails(*_args, **_kwargs):
        raise OSError("index is unwritable")

    monkeypatch.setattr(personal_mod, "_purge_label", _purge_always_fails)

    response = world.delete(world.admin_token, agent_id="shared-agent",
                            filename="notes.md")
    assert _status(response) == 200, response.data
    body = _body(response)
    # The content operation is done; the index is not. Saying "success" alone
    # would claim a consistency the store does not have.
    assert body["status"] == "pending", body
    assert body["code"] == "index_pending"
    assert body["index_state"] == "pending"
    assert not os.path.exists(world.entry("memory/notes.md"))

    assert "memory/notes.md" in personal_mod.pending_index_labels_for_root(
        world.root), "the failed purge was not recorded"

    masked = asyncio.run(manager.search("secret", include_shared=True))
    assert masked == [], "the deleted body is still retrievable"


def test_a_completed_delete_is_not_left_masked(world):
    """The converse: masking is a repair, not a permanent hiding.

    A tombstone that outlives its purge would silently remove a *recreated*
    entry from retrieval forever — the opposite bug, and a much quieter one.
    """
    from agent.memory import personal as personal_mod

    _write(world.entry("memory/notes.md"), "NOTE\n")
    world.delete(world.admin_token, agent_id="shared-agent", filename="notes.md")

    assert "memory/notes.md" not in personal_mod.pending_index_labels_for_root(
        world.root)


def test_a_refused_write_never_washes_the_journal(world, monkeypatch):
    """A refusal must not even open the write path.

    The tripwire replaces the index maintenance and the file operations: a
    refusal implemented by "write then undo" would trip one of them. Every way a
    write can be refused is walked here, so the property is about refusals in
    general and not about one branch of the guard.
    """
    from agent.memory import personal as personal_mod
    from agent.memory.service import MemoryService

    path = _write(world.entry(), "ORIGINAL\n")
    for name in ("_purge_label", "_index_label"):
        monkeypatch.setattr(personal_mod, name, _boom)
    monkeypatch.setattr(MemoryService, "save", _boom)
    monkeypatch.setattr(MemoryService, "delete", _boom)

    refusals = (
        (world.bob_token, "alice-agent", "not_owner"),         # not the owner
        (world.admin_token, "alice-agent", "not_owner"),       # admin, not owner
        (world.alice_token, "alice-agent", "not_authorized"),  # root reads shared
        (world.alice_token, "shared-agent", "not_authorized"),  # shared scope
    )
    for token, agent_id, code in refusals:
        response = world.save(token, agent_id=agent_id, content="REFUSED\n")
        assert _status(response) == 403, response.data
        assert _body(response)["code"] == code, (token, agent_id, response.data)
        assert _read(path) == "ORIGINAL\n"
