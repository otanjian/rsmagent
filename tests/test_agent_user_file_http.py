# encoding:utf-8
"""Two members sharing one Agent on the real Wire (task 3.4).

change ``isolate-shared-agent-user-data``. The unit tests
(``test_agent_user_file_access.py``) exercise the policy functions directly; what
only the real WSGI app can answer is whether every *route* actually applies that
policy -- the route policy, the tenant-from-resource derivation, the session
cookie and the handler's own scope all sit between the rule and the bytes.

The fixture is the product's own shape: Alice and Bob are two ``member``s of one
tenant sharing one Agent, and the Agent lives inside the tenant's shared root so
the console's tree/search (rooted there) can address it by a relative path.
"""

import json
import os
from urllib.parse import quote

import pytest

from tests._helpers import WebAppHarness

AGENT = "shared-agent"


@pytest.fixture()
def web(tmp_path):
    harness = WebAppHarness(tmp_path)
    # An Agent workspace nested under the tenant shared root, the layout the
    # console's tree/search walk from. A roster entry may carry the workspace
    # explicitly; ``add_agent`` keeps that mapping instead of replacing it.
    harness.workspace = os.path.join(harness.shared_root, "agents", AGENT)
    harness.write_roster([
        {"id": AGENT, "name": "Shared", "workspace": harness.workspace},
    ])
    harness.add_agent(AGENT)
    harness.alice = harness.member("alice", ["member"])
    harness.bob = harness.member("bob", ["member"])
    yield harness
    harness.close()


def seed(web, user_id, *parts, content=b"payload"):
    """Write one file into ``user/<user_id>/...`` of the shared Agent."""
    path = os.path.join(web.workspace, "user", user_id, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def seeded_users(web):
    """A fresh ``user/<alice>`` and ``user/<bob>`` pair with distinct bytes."""
    alice_file = seed(web, web.alice, "uploads", "a.txt", content=b"alice-private")
    bob_file = seed(web, web.bob, "uploads", "b.txt", content=b"bob-private")
    return alice_file, bob_file


def body_of(response):
    return response.data.decode("utf-8", "replace")


def test_a_member_reads_their_own_file_and_not_a_colleagues(web):
    alice_file, bob_file = seeded_users(web)
    alice = web.login("alice")

    own = web.get("/api/file?path=" + quote(alice_file), token=alice, tenant=False)
    assert own.status.startswith("200"), body_of(own)
    assert own.data == b"alice-private"
    # A member's file must not be retained by a shared cache.
    assert "private, no-store" in own.headers.get("Cache-Control", "")

    other = web.get("/api/file?path=" + quote(bob_file), token=alice, tenant=False)
    assert not other.status.startswith("200"), body_of(other)
    assert b"bob-private" not in other.data


def test_a_platform_admin_is_refused_a_members_file(web):
    """Holding the platform qualification is not the file's ownership."""
    _, bob_file = seeded_users(web)

    response = web.get("/api/file?path=" + quote(bob_file),
                       token=web.login("root"), tenant=False)
    assert not response.status.startswith("200"), body_of(response)
    assert b"bob-private" not in response.data


def test_the_uploads_subresource_serves_each_member_their_own(web):
    """The same file name in two members' subtrees must not collide."""
    seed(web, web.alice, "uploads", "thumb.png", content=b"alice-thumb")
    seed(web, web.bob, "uploads", "thumb.png", content=b"bob-thumb")
    path = "/uploads/thumb.png?agent_id=" + AGENT

    alice = web.get(path, token=web.login("alice"), tenant=False)
    assert alice.status.startswith("200"), body_of(alice)
    assert alice.data == b"alice-thumb"

    bob = web.get(path, token=web.login("bob"), tenant=False)
    assert bob.status.startswith("200"), body_of(bob)
    assert bob.data == b"bob-thumb"


def test_the_tree_lists_the_container_but_hides_the_colleague(web):
    alice_file, bob_file = seeded_users(web)

    def names(token):
        response = web.get(
            "/api/workspace/tree?path=agents/%s/user&agent=%s" % (AGENT, AGENT),
            token=token)
        assert response.status.startswith("200"), body_of(response)
        return {entry["name"] for entry in json.loads(response.data)["entries"]}

    assert names(web.login("alice")) == {web.alice}
    assert names(web.login("bob")) == {web.bob}


def test_search_never_returns_a_colleagues_file(web):
    seed(web, web.alice, "work", "needle_alice.txt", content=b"needle alice")
    seed(web, web.bob, "work", "needle_bob.txt", content=b"needle bob")

    response = web.get(
        "/api/workspace/search?q=needle&agent=" + AGENT, token=web.login("alice"))
    assert response.status.startswith("200"), body_of(response)
    text = body_of(response)
    assert "needle_alice" in text
    assert "needle_bob" not in text


def test_resolve_refuses_a_colleagues_file(web):
    _, bob_file = seeded_users(web)

    response = web.get(
        "/api/workspace/resolve?agent=%s&path=%s" % (AGENT, quote(bob_file)),
        token=web.login("alice"))
    assert response.status.split()[0] in ("403", "404"), body_of(response)
    assert b"preview_url" not in response.data
    assert b"bob-private" not in response.data


def test_workspace_read_refuses_a_colleagues_file(web):
    _, bob_file = seeded_users(web)
    rel = "agents/%s/user/%s/uploads/b.txt" % (AGENT, web.bob)

    response = web.get(
        "/api/workspace/read?agent=%s&path=%s" % (AGENT, rel),
        token=web.login("alice"))
    assert not response.status.startswith("200"), body_of(response)
    assert b"bob-private" not in response.data


def test_workspace_write_refuses_a_colleagues_file_and_changes_nothing(web):
    _, bob_file = seeded_users(web)
    rel = "agents/%s/user/%s/uploads/b.txt" % (AGENT, web.bob)

    response = web.post("/api/workspace/write",
                        {"path": rel, "content": "tampered"},
                        token=web.login("alice"))
    assert not response.status.startswith("200"), body_of(response)
    with open(bob_file, "rb") as handle:
        assert handle.read() == b"bob-private"


def test_preview_of_a_members_file_needs_that_member(web):
    from channel.web import web_channel

    alice_file, _ = seeded_users(web)
    url = web_channel._build_preview_url(os.path.realpath(alice_file))

    anonymous = web.get(url, tenant=False)
    assert anonymous.status.split()[0] == "404", body_of(anonymous)

    colleague = web.get(url, token=web.login("bob"), tenant=False)
    assert colleague.status.split()[0] == "404", body_of(colleague)

    owner = web.get(url, token=web.login("alice"), tenant=False)
    assert owner.status.startswith("200"), body_of(owner)
    assert owner.data == b"alice-private"
    assert "private, no-store" in owner.headers.get("Cache-Control", "")


def test_the_tree_refuses_a_colleagues_directory_instead_of_a_blank_listing(web):
    """An empty listing still answers "the path exists"; the route must refuse.

    Filtering the *entries* of ``user/<bob>`` hides the file names but not the
    directory itself: a 200 with ``entries: []`` reads differently from the
    error a nonexistent path returns, which is the path-existence leak the spec
    forbids. The addressed directory is subject to the same ownership rule as
    the entries inside it.
    """
    seeded_users(web)
    colleague = "agents/%s/user/%s" % (AGENT, web.bob)

    response = web.get(
        "/api/workspace/tree?path=%s&agent=%s" % (colleague, AGENT),
        token=web.login("alice"))
    assert response.status.split()[0] in ("403", "404"), body_of(response)
    assert web.bob.encode() not in response.data
    assert b"uploads" not in response.data


def test_the_tree_still_lists_the_container_and_the_agent_workspace(web):
    """The refusal above must not close the panel's own landing points."""
    seeded_users(web)
    alice = web.login("alice")

    def listing(path):
        response = web.get(
            "/api/workspace/tree?path=%s&agent=%s" % (path, AGENT), token=alice)
        assert response.status.startswith("200"), body_of(response)
        return {entry["name"] for entry in json.loads(response.data)["entries"]}

    assert listing("agents/%s" % AGENT) >= {"user"}
    assert listing("agents/%s/user" % AGENT) == {web.alice}
    assert listing("agents/%s/user/%s" % (AGENT, web.alice)) == {"uploads"}


def test_a_dot_dot_path_cannot_reach_a_colleagues_directory(web):
    """``..`` must resolve to the colleague's path, not to the caller's own.

    The rule is applied to the *real* path, so a relative hop out of the
    caller's own folder into a colleague's is judged by where it lands, not by
    the fact that the typed path started inside the caller's own subtree.
    """
    seeded_users(web)
    alice = web.login("alice")

    tree = web.get(
        "/api/workspace/tree?path=%s&agent=%s" % (
            quote("agents/%s/user/%s/../%s" % (AGENT, web.alice, web.bob)),
            AGENT),
        token=alice)
    assert tree.status.split()[0] in ("403", "404"), body_of(tree)
    assert b"uploads" not in tree.data

    read = web.get(
        "/api/workspace/read?agent=%s&path=%s" % (
            AGENT,
            quote("agents/%s/user/%s/../%s/uploads/b.txt"
                  % (AGENT, web.alice, web.bob))),
        token=alice)
    assert not read.status.startswith("200"), body_of(read)
    assert b"bob-private" not in read.data


def test_an_ordinary_shared_file_still_serves_the_whole_tenant(web):
    shared_file = os.path.join(web.workspace, "reports", "q1.txt")
    os.makedirs(os.path.dirname(shared_file), exist_ok=True)
    with open(shared_file, "wb") as handle:
        handle.write(b"shared-report")

    for username in ("alice", "bob", "root"):
        response = web.get("/api/file?path=" + quote(shared_file),
                           token=web.login(username), tenant=False)
        assert response.status.startswith("200"), body_of(response)
        assert response.data == b"shared-report"
