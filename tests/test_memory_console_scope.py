# encoding:utf-8
"""Task 5: ``GET /api/memory`` and ``GET /api/memory/content`` as a
*scope-adapted compatibility read surface* (change
``complete-database-capability-parity``).

The legacy console page reads two upstream endpoints:

* ``GET /api/memory`` — page/paginated metadata list;
* ``GET /api/memory/content`` — the body of one entry.

Upstream answered both from the *Agent workspace* (``state_root``). The fork's
memory truth has three different shapes, so the surface keeps the old field
names but requires an explicit target: ``scope=personal`` (the caller's own
``/api/memory/personal`` domain), ``scope=private_agent`` (a privately owned
Agent's workspace memory, owner-only) or ``scope=shared`` (the tenant's shared
Agent memory, which is a *tenant resource* and so needs the tenant
administration qualification). A missing/unknown target is refused with a
stable machine code — never silently answered from the tenant shared root, and
never read-then-filtered.

Every test here drives the real ``build_web_app()`` application through the
``web_app`` fixture: the gate, the resolver and the storage have to agree, and
only a WSGI request shows all three. A direct handler call would prove none of
it.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from tests._helpers import IdentityStack

TEMP = IdentityStack.TEMP_PASSWORD
MEMBER = IdentityStack.MEMBER_PASSWORD
ADMIN = IdentityStack.ROOT_PASSWORD


# --- small helpers ----------------------------------------------------------

def _status(response) -> int:
    return int(response.status.split()[0])


def _body(response) -> dict:
    return json.loads(response.data.decode("utf-8"))


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _boom(*_args, **_kwargs):
    """Stand-in for a memory read that a refusal must never reach."""
    raise AssertionError("the memory service was read before the refusal")


def _member_in(harness, tenant_id: str, username: str, *, operation="create-new"):
    """Add a member to ``tenant_id`` (any tenant), ready to log in."""
    created = harness.service.create_member(
        actor_user_id=harness.admin_id, tenant_id=tenant_id, operation=operation,
        username=username, display_name=username.title(),
        temporary_password=TEMP, roles=["member"])
    if operation == "create-new":
        harness.service.change_password(
            harness.service.login(username, TEMP).token, TEMP, MEMBER)
    return created["user_id"]


class _World:
    """The identity fixture the compatibility surface is judged against.

    One tenant (``acme``) with a shared Agent, two members each owning a private
    Agent, and a tenant administrator who owns nothing; plus a second tenant
    whose member must never reach acme, and the *same* account ``alice`` bound
    to both tenants.
    """

    def __init__(self, harness):
        self.h = harness
        self.tenant_id = harness.tenant_id
        self.shared_root = harness.shared_root
        harness.add_agent("shared-agent", "alice-agent", "bob-agent")

        self.alice = harness.member("alice", ["member"])
        self.bob = harness.member("bob", ["member"])
        self.admin = harness.member("admin", ["tenant_admin"])
        self.alice_token = harness.login("alice")
        self.bob_token = harness.login("bob")
        self.admin_token = harness.login("admin")

        self.bind_private("alice-agent", self.alice)
        self.bind_private("bob-agent", self.bob)

        # A second tenant, and one account that belongs to both (the same
        # ``alice`` user id, a second membership).
        self.globex = harness.service.create_tenant(
            actor_user_id=harness.admin_id, code="globex", name="Globex",
            shared_root=os.path.join(harness.root, "tenants", "globex"),
            admin_username="globex-root", admin_display="Globex Root",
            admin_password=ADMIN, recent_password=ADMIN)["id"]
        self.glenda = _member_in(harness, self.globex, "glenda")
        self.glenda_token = harness.service.login("glenda", MEMBER).token
        harness.service.create_member(
            actor_user_id=harness.admin_id, tenant_id=self.globex,
            operation="bind-existing", username="alice", display_name="Alice",
            temporary_password=TEMP, roles=["member"])

    # -- fixture population --------------------------------------------------

    def bind_private(self, agent_id: str, owner_user_id: str) -> None:
        self.h.service.bind_agent(tenant_id=self.tenant_id, agent_id=agent_id,
                                  private_owner_user_id=owner_user_id)

    def save_personal(self, token: str, entry_id: str, content: str) -> None:
        """Seed one personal entry through the *delivered* write path."""
        response = self.h.post("/api/memory/personal",
                               {"action": "save", "id": entry_id,
                                "content": content}, token=token)
        assert _status(response) == 200, response.data

    def personal_root(self, user_id: str) -> str:
        return os.path.join(self.shared_root, "users", user_id)

    def root_outside(self) -> str:
        """A directory outside every tenant root, for out-of-root symlinks."""
        path = os.path.join(self.h.root, "outside")
        os.makedirs(path, exist_ok=True)
        return path

    def seed_agent_memory(self, name: str, content: str) -> str:
        """A file the agent-scope ``MemoryService`` reads: the tenant root.

        In database mode the fork resolves the memory root from the *tenant*
        (``channel/web/tenant_workspace.py``), not from the Agent's own
        workspace directory, so the shared and private Agent scopes read the
        same files and only the owner check separates them.
        """
        return _write(os.path.join(self.shared_root, name), content)

    def get(self, path: str, token: str = None, *, tenant: str = None):
        headers = {"X-Tenant-ID": tenant} if tenant else None
        return self.h.get(path, token=token, headers=headers)

    def set_agent_enabled(self, agent_id: str, enabled: bool) -> None:
        """Stop/start an Agent in the roster the registry actually reads.

        The registry is built from the pinned settings and cached process-wide,
        so the flag is flipped in that same roster file and the cache dropped —
        editing a file the registry does not read would assert against a stale
        roster.
        """
        from agent import team
        from agent.registry import set_agent_registry

        path = team.team_file(self.h._settings)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for entry in data.get("agents", []):
            if entry.get("id") == agent_id:
                entry["enabled"] = bool(enabled)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        set_agent_registry(None)

    def targets(self, token: str):
        body = self.get("/api/memory?scope=personal", token=token)
        assert _status(body) == 200, body.data
        return _body(body)["targets"]


@pytest.fixture
def world(web_app):
    return _World(web_app("memory-console"))


# --- the positive path ------------------------------------------------------

def test_a_member_lists_and_reads_their_own_personal_memory(world):
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")
    world.save_personal(world.alice_token, "memory/notes.md", "ALICE-NOTE\n")

    listed = world.get("/api/memory?scope=personal", token=world.alice_token)
    assert _status(listed) == 200, listed.data
    body = _body(listed)
    assert body["status"] == "success"
    assert body["scope"] == "personal"
    # Legacy pagination envelope and per-row fields the console reads.
    assert (body["page"], body["page_size"], body["total"]) == (1, 20, 2)
    rows = {row["filename"]: row for row in body["list"]}
    assert set(rows) == {"MEMORY.md", "memory/notes.md"}
    assert rows["MEMORY.md"]["type"] == "global"
    assert rows["memory/notes.md"]["type"] == "daily"
    for row in rows.values():
        assert row["size"] > 0
        assert row["updated_at"]
        assert row["revision"]
        assert row["actions"] == {"edit": True, "delete": True}, "owner management surface"

    content = world.get("/api/memory/content?scope=personal&filename=MEMORY.md",
                        token=world.alice_token)
    assert _status(content) == 200, content.data
    read = _body(content)
    assert read["status"] == "success"
    assert read["filename"] == "MEMORY.md"
    assert read["content"] == "ALICE-THESIS\n"
    assert read["revision"] == rows["MEMORY.md"]["revision"]
    assert read["scope"] == "personal"
    assert read["rel_path"] == "MEMORY.md"


def test_the_legacy_shape_without_a_scope_reads_my_own_personal_memory(world):
    """No ``scope`` and no ``agent_id`` means *me* — never the tenant root."""
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")
    world.seed_agent_memory("MEMORY.md", "TENANT-SHARED-MEMORY\n")

    response = world.get("/api/memory", token=world.alice_token)
    assert _status(response) == 200, response.data
    body = _body(response)
    assert body["scope"] == "personal"
    assert [row["filename"] for row in body["list"]] == ["MEMORY.md"]

    content = world.get("/api/memory/content?filename=MEMORY.md",
                        token=world.alice_token)
    assert _body(content)["content"] == "ALICE-THESIS\n"


def test_a_missing_scope_with_an_agent_reads_that_agents_memory(world):
    world.seed_agent_memory("MEMORY.md", "SHARED-AGENT-MEMORY\n")
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")

    response = world.get("/api/memory?agent_id=shared-agent", token=world.admin_token)
    assert _status(response) == 200, response.data
    body = _body(response)
    assert body["scope"] == "shared"
    assert body["agent_id"] == "shared-agent"
    assert [row["filename"] for row in body["list"]] == ["MEMORY.md"]

    content = world.get(
        "/api/memory/content?agent_id=shared-agent&filename=MEMORY.md",
        token=world.admin_token)
    assert _body(content)["content"] == "SHARED-AGENT-MEMORY\n"


def test_a_member_is_refused_a_shared_target_even_with_chat_use(world, monkeypatch):
    """A shared Agent's memory is a tenant resource, not a member resource.

    ``alice`` may chat with ``shared-agent`` (it is the tenant's shared Agent),
    but chat use is not management access: the target set is filtered on the
    server, so she gets no entries, no total and no body. The tripwire pins that
    the refusal happens *before* the read rather than as an after-the-fact
    filter.
    """
    from agent.memory.service import MemoryService

    world.seed_agent_memory("MEMORY.md", "SHARED-AGENT-MEMORY\n")
    monkeypatch.setattr(MemoryService, "list_files", _boom)
    monkeypatch.setattr(MemoryService, "get_content", _boom)

    for path in ("/api/memory?agent_id=shared-agent",
                 "/api/memory?scope=shared&agent_id=shared-agent",
                 "/api/memory/content?scope=shared&agent_id=shared-agent"
                 "&filename=MEMORY.md"):
        response = world.get(path, token=world.alice_token)
        assert _status(response) == 403, (path, response.data)
        assert _body(response)["code"] == "not_authorized"
        assert "SHARED-AGENT-MEMORY" not in response.data.decode("utf-8")


def test_pagination_is_honoured(world):
    world.save_personal(world.alice_token, "MEMORY.md", "one\n")
    world.save_personal(world.alice_token, "memory/a.md", "two\n")
    world.save_personal(world.alice_token, "memory/b.md", "three\n")

    response = world.get("/api/memory?scope=personal&page=2&page_size=2",
                         token=world.alice_token)
    body = _body(response)
    assert body["total"] == 3
    assert body["page"] == 2
    assert len(body["list"]) == 1

    bad = world.get("/api/memory?scope=personal&page_size=0", token=world.alice_token)
    assert _status(bad) == 400
    assert _body(bad)["code"] == "invalid_paging"


# --- private Agent memory stays private -------------------------------------

def test_the_owner_reads_their_private_agents_memory(world):
    world.seed_agent_memory("MEMORY.md", "PRIVATE-WORKSPACE-MEMORY\n")
    response = world.get("/api/memory?scope=private_agent&agent_id=alice-agent",
                         token=world.alice_token)
    assert _status(response) == 200, response.data
    body = _body(response)
    assert body["scope"] == "private_agent"
    assert body["agent_id"] == "alice-agent"

    content = world.get(
        "/api/memory/content?scope=private_agent&agent_id=alice-agent"
        "&filename=MEMORY.md", token=world.alice_token)
    assert _status(content) == 200, content.data
    assert _body(content)["content"] == "PRIVATE-WORKSPACE-MEMORY\n"


@pytest.mark.parametrize("who", ["bob", "admin"])
def test_a_non_owner_is_refused_before_any_read(world, monkeypatch, who):
    """A same-tenant other member *and* the tenant administrator.

    The memory service is replaced by a tripwire: a refusal that read the
    workspace first and filtered afterwards would trip it.
    """
    from agent.memory.service import MemoryService

    world.seed_agent_memory("MEMORY.md", "PRIVATE-WORKSPACE-MEMORY\n")
    monkeypatch.setattr(MemoryService, "list_files", _boom)
    monkeypatch.setattr(MemoryService, "get_content", _boom)
    token = getattr(world, "%s_token" % who)

    listed = world.get("/api/memory?scope=private_agent&agent_id=alice-agent",
                       token=token)
    assert _status(listed) == 403, listed.data
    assert _body(listed)["code"] == "not_owner"
    assert "PRIVATE-WORKSPACE-MEMORY" not in listed.data.decode("utf-8")

    content = world.get(
        "/api/memory/content?scope=private_agent&agent_id=alice-agent"
        "&filename=MEMORY.md", token=token)
    assert _status(content) == 403, content.data
    assert _body(content)["code"] == "not_owner"
    assert "PRIVATE-WORKSPACE-MEMORY" not in content.data.decode("utf-8")


def test_the_legacy_agent_id_shape_keeps_the_same_owner_gate(world):
    world.seed_agent_memory("MEMORY.md", "PRIVATE-WORKSPACE-MEMORY\n")
    response = world.get("/api/memory?agent_id=alice-agent", token=world.bob_token)
    assert _status(response) == 403, response.data
    assert _body(response)["code"] == "not_owner"


# --- cross tenant -----------------------------------------------------------

def test_a_member_of_another_tenant_is_refused(world):
    """Selecting a tenant the account is not a member of is refused outright."""
    response = world.get("/api/memory?scope=shared&agent_id=shared-agent",
                         token=world.glenda_token, tenant=world.tenant_id)
    assert _status(response) == 403, response.data
    assert "SHARED" not in response.data.decode("utf-8")


def test_another_tenants_agent_is_not_addressable(world, monkeypatch):
    from agent.memory.service import MemoryService

    world.seed_agent_memory("MEMORY.md", "ACME-ONLY\n")
    monkeypatch.setattr(MemoryService, "list_files", _boom)
    monkeypatch.setattr(MemoryService, "get_content", _boom)

    response = world.get("/api/memory?scope=shared&agent_id=shared-agent",
                         token=world.glenda_token, tenant=world.globex)
    assert _status(response) in (403, 404), response.data
    assert "ACME-ONLY" not in response.data.decode("utf-8")


def test_the_same_account_bound_to_another_tenant_reads_only_its_own(world):
    """``alice`` is a member of both tenants: one account, two domains."""
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-ACME\n")
    # Her globex personal root is a different directory and starts empty.
    globex = world.get("/api/memory?scope=personal", token=world.alice_token,
                       tenant=world.globex)
    assert _status(globex) == 200, globex.data
    assert _body(globex)["total"] == 0
    assert "ALICE-ACME" not in globex.data.decode("utf-8")

    # And acme's private Agent is not addressable from her globex context: the
    # binding names acme, so the target does not resolve at all.
    foreign = world.get("/api/memory?scope=private_agent&agent_id=alice-agent",
                        token=world.alice_token, tenant=world.globex)
    assert _status(foreign) == 404, foreign.data
    assert _body(foreign)["code"] == "unknown_agent"

    own = world.get("/api/memory?scope=personal", token=world.alice_token)
    assert _body(own)["total"] == 1


# --- refuses rather than guesses --------------------------------------------

def test_an_unknown_scope_is_refused(world):
    for path in ("/api/memory?scope=everything",
                 "/api/memory/content?scope=everything&filename=MEMORY.md"):
        response = world.get(path, token=world.alice_token)
        assert _status(response) == 400, (path, response.data)
        assert _body(response)["code"] == "unknown_scope"
        assert _body(response)["status"] == "error"


def test_an_unknown_category_is_refused(world):
    for path in ("/api/memory?scope=personal&category=dreams",
                 "/api/memory?scope=personal&category=invalid",
                 "/api/memory/content?scope=personal&category=invalid"
                 "&filename=MEMORY.md"):
        response = world.get(path, token=world.alice_token)
        assert _status(response) == 400, (path, response.data)
        assert _body(response)["code"] == "unknown_category"


def test_a_known_category_is_still_served_for_an_agent(world):
    world.seed_agent_memory("memory/dreams/2026-01-02.md", "DREAM\n")
    response = world.get(
        "/api/memory?scope=shared&agent_id=shared-agent&category=dream",
        token=world.admin_token)
    assert _status(response) == 200, response.data
    assert [row["filename"] for row in _body(response)["list"]] == ["2026-01-02.md"]


def test_the_personal_scope_refuses_an_agent_target(world):
    response = world.get("/api/memory?scope=personal&agent_id=alice-agent",
                         token=world.alice_token)
    assert _status(response) == 400, response.data
    assert _body(response)["code"] == "ambiguous_target"


def test_a_content_request_without_an_entry_is_refused(world):
    response = world.get("/api/memory/content?scope=personal",
                         token=world.alice_token)
    assert _status(response) == 400, response.data
    assert _body(response)["code"] == "entry_required"


def test_an_unknown_entry_is_refused(world):
    world.seed_agent_memory("MEMORY.md", "SHARED\n")
    response = world.get(
        "/api/memory/content?scope=shared&agent_id=shared-agent&filename=absent.md",
        token=world.admin_token)
    assert _status(response) == 404, response.data
    assert _body(response)["code"] == "unknown_entry"


def test_a_malformed_personal_entry_id_is_refused(world):
    for entry in ("../MEMORY.md", "/etc/passwd", "memory/notes.txt", "notes.md"):
        response = world.get(
            "/api/memory/content?scope=personal&filename=%s" % entry,
            token=world.alice_token)
        assert _status(response) == 400, (entry, response.data)
        assert _body(response)["code"] == "invalid_entry"


def test_the_gate_still_refuses_an_anonymous_caller(world):
    response = world.get("/api/memory?scope=personal")
    assert _status(response) in (401, 403), response.data
    assert _status(response) != 503, "the legacy 503 must be gone"


# --- symlinks ---------------------------------------------------------------

def test_an_entry_symlink_out_of_the_root_is_refused(world):
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")
    root = world.personal_root(world.alice)
    target = _write(os.path.join(world.root_outside(), "target.md"), "OUTSIDE-SECRET\n")
    os.makedirs(os.path.join(root, "memory"), exist_ok=True)
    os.symlink(target, os.path.join(root, "memory", "notes.md"))

    listed = world.get("/api/memory?scope=personal", token=world.alice_token)
    assert _status(listed) == 200, listed.data
    assert "OUTSIDE-SECRET" not in listed.data.decode("utf-8")
    assert [row["filename"] for row in _body(listed)["list"]] == ["MEMORY.md"]

    content = world.get(
        "/api/memory/content?scope=personal&filename=memory/notes.md",
        token=world.alice_token)
    assert _status(content) == 403, content.data
    assert _body(content)["code"] == "unsafe_path"
    assert "OUTSIDE-SECRET" not in content.data.decode("utf-8")


def test_an_intermediate_directory_symlink_is_refused(world):
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")
    world.save_personal(world.alice_token, "memory/mine.md", "ALICE-NOTE\n")
    world.save_personal(world.bob_token, "memory/notes.md", "BOB-SECRET\n")
    alice_root = world.personal_root(world.alice)
    bob_root = world.personal_root(world.bob)

    shutil.rmtree(os.path.join(alice_root, "memory"))
    os.symlink(os.path.join(bob_root, "memory"), os.path.join(alice_root, "memory"))

    listed = world.get("/api/memory?scope=personal", token=world.alice_token)
    assert _status(listed) == 403, listed.data
    assert "BOB-SECRET" not in listed.data.decode("utf-8")
    assert _body(listed)["code"] == "unsafe_path"

    content = world.get(
        "/api/memory/content?scope=personal&filename=memory/notes.md",
        token=world.alice_token)
    assert _status(content) == 403, content.data
    assert _body(content)["code"] == "unsafe_path"
    assert "BOB-SECRET" not in content.data.decode("utf-8")


def test_a_symlinked_personal_root_is_refused(world):
    world.save_personal(world.bob_token, "MEMORY.md", "BOB-SECRET\n")
    alice_root = world.personal_root(world.alice)
    os.makedirs(os.path.dirname(alice_root), exist_ok=True)
    os.symlink(world.personal_root(world.bob), alice_root)

    listed = world.get("/api/memory?scope=personal", token=world.alice_token)
    assert _status(listed) == 403, listed.data
    assert _body(listed)["code"] == "unsafe_path"
    assert "BOB-SECRET" not in listed.data.decode("utf-8")

    content = world.get("/api/memory/content?scope=personal&filename=MEMORY.md",
                        token=world.alice_token)
    assert _status(content) == 403, content.data
    assert "BOB-SECRET" not in content.data.decode("utf-8")


def test_both_personal_entries_refuse_the_same_symlinked_entry(world):
    """The boundary lives in the shared service, not in this surface.

    The accessor spec requires that one out-of-root target is answered the same
    way by the recovered compatibility entry and by the already-open
    ``/api/memory/personal*`` entry -- a fix that only hardened one of them
    would show up here as the two answers diverging.
    """
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")
    root = world.personal_root(world.alice)
    target = _write(os.path.join(world.root_outside(), "target.md"), "OUTSIDE-SECRET\n")
    os.makedirs(os.path.join(root, "memory"), exist_ok=True)
    os.symlink(target, os.path.join(root, "memory", "notes.md"))

    compat = world.get(
        "/api/memory/content?scope=personal&filename=memory/notes.md",
        token=world.alice_token)
    delivered = world.get("/api/memory/personal/content?id=memory/notes.md",
                          token=world.alice_token)

    for response in (compat, delivered):
        assert _status(response) == 403, response.data
        assert "OUTSIDE-SECRET" not in response.data.decode("utf-8")
    assert _body(compat)["code"] == _body(delivered)["code"], compat.data + delivered.data


def test_a_symlinked_agent_memory_directory_is_not_followed(world):
    """The agent scope's root is the whole tenant root, so a planted ``memory``
    symlink pointing at a member's personal directory must not be followed.

    Without the guard this is a real cross-member read, not a naming quirk:
    ``MemoryService`` resolves a name against ``<root>/memory`` and contains it
    against that *symlinked* directory, so ``notes.md`` would return the
    member's own note.
    """
    world.save_personal(world.alice_token, "memory/notes.md", "ALICE-PERSONAL\n")
    os.symlink(os.path.join(world.personal_root(world.alice), "memory"),
               os.path.join(world.shared_root, "memory"))

    listed = world.get("/api/memory?scope=shared&agent_id=shared-agent",
                       token=world.admin_token)
    payload = listed.data.decode("utf-8")
    assert "ALICE-PERSONAL" not in payload
    assert "notes.md" not in payload, payload
    assert _status(listed) == 403, listed.data
    assert _body(listed)["code"] == "unsafe_path"

    content = world.get(
        "/api/memory/content?scope=shared&agent_id=shared-agent&filename=notes.md",
        token=world.admin_token)
    assert _status(content) == 403, content.data
    assert _body(content)["code"] == "unsafe_path"
    assert "ALICE-PERSONAL" not in content.data.decode("utf-8")


def test_a_symlinked_agent_entry_is_not_served(world):
    """A symlinked *entry* is dropped from the listing and refused on read.

    The out-of-``memory/`` target is the interesting one: the delivered
    ``MemoryService`` containment check compares against the (symlinked)
    directory, so the surface is the layer that has to refuse it.
    """
    world.seed_agent_memory("MEMORY.md", "SHARED-REAL\n")
    world.save_personal(world.bob_token, "memory/notes.md", "BOB-SECRET\n")
    os.makedirs(os.path.join(world.shared_root, "memory"), exist_ok=True)
    os.symlink(os.path.join(world.personal_root(world.bob), "memory", "notes.md"),
               os.path.join(world.shared_root, "memory", "uploaded.md"))

    listed = world.get("/api/memory?scope=shared&agent_id=shared-agent",
                       token=world.admin_token)
    assert _status(listed) == 200, listed.data
    assert [row["filename"] for row in _body(listed)["list"]] == ["MEMORY.md"]
    assert "BOB-SECRET" not in listed.data.decode("utf-8")

    content = world.get(
        "/api/memory/content?scope=shared&agent_id=shared-agent"
        "&filename=uploaded.md", token=world.admin_token)
    assert _status(content) == 403, content.data
    assert _body(content)["code"] == "unsafe_path"
    assert "BOB-SECRET" not in content.data.decode("utf-8")


# --- the delivered personal surface is untouched ----------------------------

def test_the_personal_endpoints_still_behave(world):
    world.save_personal(world.alice_token, "memory/notes.md", "ALICE-NOTE\n")
    listed = world.get("/api/memory/personal", token=world.alice_token)
    assert _status(listed) == 200, listed.data
    assert [row["id"] for row in _body(listed)["entries"]] == ["memory/notes.md"]
    assert _body(listed)["scope"] == "personal"

    content = world.get("/api/memory/personal/content?id=memory/notes.md",
                        token=world.alice_token)
    assert _status(content) == 200, content.data
    assert _body(content)["content"] == "ALICE-NOTE\n"


def test_the_compat_personal_scope_does_not_expose_another_member(world):
    world.save_personal(world.alice_token, "MEMORY.md", "ALICE-THESIS\n")
    world.save_personal(world.bob_token, "MEMORY.md", "BOB-SECRET\n")

    response = world.get("/api/memory?scope=personal&page_size=100",
                         token=world.alice_token)
    payload = response.data.decode("utf-8")
    assert _status(response) == 200, response.data
    assert "ALICE-THESIS" not in payload  # metadata only, and only mine
    assert "BOB-SECRET" not in payload
    assert [row["filename"] for row in _body(response)["list"]] == ["MEMORY.md"]

    bob = world.get("/api/memory/content?scope=personal&filename=MEMORY.md",
                    token=world.bob_token)
    assert _body(bob)["content"] == "BOB-SECRET\n"


# --- the registry really opens these reads ----------------------------------

def test_the_capability_registry_and_the_route_table_agree(world):
    from auth import capability_matrix
    from channel.web.route_registry import derive_route_policy

    spec = capability_matrix.slice_for("memory_browse")
    # Two reads and the three writes of task 5.1's second half. The writes are
    # ``config``, not ``execute``: they write stored data, while ``execute`` in
    # this registry is for actions that run something (a tool import) — and the
    # page must not be reported as having opened an execution surface.
    assert spec.open == {"list": "read", "content": "read",
                         "save": "config", "delete": "config",
                         "clear": "config"}
    assert spec.implemented and spec.accepted
    assert capability_matrix.check_consistency() == []

    policy = derive_route_policy()
    for path in ("/api/memory", "/api/memory/content"):
        entry = policy[path]["GET"]
        assert entry["policy"] == "tenant", path
        assert entry["permission"] == "memory.read", path
    # Each write is its own route, so the gate can close one verb without
    # closing the others; a verb the registry serves but the table does not (or
    # the reverse) would be a route reachable without the policy it declares.
    for path, action in (("/api/memory/save", "save"),
                         ("/api/memory/delete", "delete"),
                         ("/api/memory/clear", "clear")):
        entry = policy[path]["POST"]
        assert entry["policy"] == "tenant", path
        assert entry["permission"] == "memory.read", path
        assert capability_matrix.slice_for("memory_browse").route(
            action, comment="")["permission"] == "memory.read", action


# --- the legal target set the picker is built from (task 5.1) ---------------

def test_the_target_set_names_only_domains_the_caller_may_address(world):
    """The set is the *management* range, not the chat/use range.

    ``alice`` may chat with the tenant's ``shared-agent``, so a picker built
    from the console's Agent catalogue would offer it — and the read would then
    refuse it. The set is derived from the same predicate the read runs, so it
    offers her own private Agent and her personal domain, and neither the shared
    Agent nor another member's private one.
    """
    by_kind = {(row["kind"], row.get("agent_id")): row
               for row in world.targets(world.alice_token)}
    assert ("personal", None) in by_kind
    assert ("agent", "alice-agent") in by_kind
    assert ("agent", "shared-agent") not in by_kind, "a member does not manage shared memory"
    assert ("agent", "bob-agent") not in by_kind, "another member's private Agent is not a target"


def test_an_administrator_target_set_holds_the_shared_agents_and_their_own(world):
    by_kind = {(row["kind"], row.get("agent_id")): row
               for row in world.targets(world.admin_token)}
    assert ("personal", None) in by_kind
    assert ("agent", "shared-agent") in by_kind
    assert by_kind[("agent", "shared-agent")]["scope"] == "shared"
    assert ("agent", "alice-agent") not in by_kind, "a non-owner admin is excluded"
    assert ("agent", "bob-agent") not in by_kind


def test_every_offered_target_is_one_this_caller_can_actually_read(world):
    """The property that makes the set worth serving: offered ⇔ readable.

    A set that offers a target the read refuses is the defect it exists to
    remove, so the two are asserted together rather than separately — including
    the converse, that a *not*-offered target really is refused.
    """
    world.seed_agent_memory("MEMORY.md", "SHARED-AGENT-MEMORY\n")

    for row in world.targets(world.alice_token):
        query = ("scope=personal" if row["kind"] == "personal"
                 else f"agent_id={row['agent_id']}")
        response = world.get(f"/api/memory?{query}", token=world.alice_token)
        assert _status(response) == 200, (row, response.data)

    refused = world.get("/api/memory?agent_id=shared-agent", token=world.alice_token)
    assert _status(refused) != 200, "not offered must mean not readable"


def test_a_stopped_agent_stays_a_memory_target_and_says_so(world):
    """Stopping refuses new *traffic*, not access to what is already stored.

    An Agent that has been stopped still holds memory its owner may want to read
    or clean, so it stays a legal target — but the entry reports ``enabled:
    false`` rather than disappearing, because "I cannot see it" and "it is
    stopped" are different facts and the operator needs the second one.
    """
    world.seed_agent_memory("MEMORY.md", "ALICE-AGENT-MEMORY\n")
    world.set_agent_enabled("alice-agent", False)

    rows = {row.get("agent_id"): row for row in world.targets(world.alice_token)}
    assert "alice-agent" in rows, "a stopped object is still manageable"
    assert rows["alice-agent"]["enabled"] is False

    read = world.get("/api/memory?agent_id=alice-agent", token=world.alice_token)
    assert _status(read) == 200, "its stored memory is still readable"
