# encoding:utf-8
"""The tenant shared root's ``users/<user_id>`` tree on the real Wire.

``common.state_dir.user_root`` puts every account's private data
(``MEMORY.md``, ``PERSONA.md``, ``memory/``, ``runs/``, ``output/``) under
``<shared root>/users/<user_id>``. The console file panel's root *is* that
shared root, and the ownership rule it applied
(``classify_agent_user_path``) only knew the ``user/`` *platform files*
container inside an Agent workspace -- so the panel listed every colleague's
account directory and served its contents.

These tests drive the real WSGI app, because only the routes can answer whether
the rule reaches the tree/search/resolve/read/file surfaces.
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
    # The Agent lives inside the tenant shared root, the layout the panel walks
    # from (``_tenant_agent_workspace``); ``users/`` is its sibling.
    harness.workspace = os.path.join(harness.shared_root, "agents", AGENT)
    harness.write_roster([
        {"id": AGENT, "name": "Shared", "workspace": harness.workspace},
    ])
    harness.add_agent(AGENT)
    # The Agent's own folder is a real directory under the shared root, so the
    # root listing has a legitimate sibling next to ``users/``.
    os.makedirs(os.path.join(harness.shared_root, "agents", AGENT), exist_ok=True)
    harness.alice = harness.member("alice", ["member"])
    harness.bob = harness.member("bob", ["member"])
    yield harness
    harness.close()


def seed_account(web, user_id, *parts, content=b"private"):
    """Write one file into ``<shared root>/users/<user_id>/...``."""
    path = os.path.join(web.shared_root, "users", user_id, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def seeded_accounts(web):
    alice_file = seed_account(web, web.alice, "MEMORY.md", content=b"alice-account")
    bob_file = seed_account(web, web.bob, "MEMORY.md", content=b"bob-account")
    return alice_file, bob_file


def body_of(response):
    return response.data.decode("utf-8", "replace")


def listing_names(web, token, path=""):
    response = web.get(
        "/api/workspace/tree?path=%s&agent=%s" % (path, AGENT), token=token)
    assert response.status.startswith("200"), body_of(response)
    return {entry["name"] for entry in json.loads(response.data)["entries"]}


def test_the_shared_root_listing_does_not_offer_the_users_container(web):
    """The container's only entries are private trees, so it is not offered."""
    seeded_accounts(web)

    names = listing_names(web, web.login("alice"))
    assert "users" not in names
    assert "agents" in names


def test_the_users_container_is_refused_when_addressed_directly(web):
    seeded_accounts(web)

    response = web.get(
        "/api/workspace/tree?path=users&agent=%s" % AGENT,
        token=web.login("alice"))
    assert response.status.split()[0] in ("403", "404"), body_of(response)
    assert web.bob.encode() not in response.data


def test_a_member_cannot_list_a_colleagues_account_tree(web):
    seeded_accounts(web)

    response = web.get(
        "/api/workspace/tree?path=users/%s&agent=%s" % (web.bob, AGENT),
        token=web.login("alice"))
    assert response.status.split()[0] in ("403", "404"), body_of(response)
    assert b"MEMORY.md" not in response.data


def test_a_member_still_reaches_their_own_account_tree(web):
    """The refusal above must not close the reader's own private data."""
    seeded_accounts(web)
    alice = web.login("alice")

    assert listing_names(web, alice, "users/%s" % web.alice) == {"MEMORY.md"}
    # The container is refused, but the caller's own path under it is not: the
    # tree route is addressed directly, so this is the same admission rule.
    own = web.get("/api/workspace/resolve?agent=%s&path=%s"
                  % (AGENT, quote(os.path.join(web.shared_root, "users",
                                               web.alice, "MEMORY.md"))),
                  token=alice)
    assert own.status.startswith("200"), body_of(own)
    assert b"alice-account" not in own.data  # metadata only, no bytes


def test_a_member_cannot_read_a_colleagues_account_file(web):
    _, bob_file = seeded_accounts(web)
    alice = web.login("alice")

    served = web.get("/api/file?path=" + quote(bob_file), token=alice, tenant=False)
    assert not served.status.startswith("200"), body_of(served)
    assert b"bob-account" not in served.data

    read = web.get("/api/workspace/read?agent=%s&path=users/%s/MEMORY.md"
                   % (AGENT, web.bob), token=alice)
    assert not read.status.startswith("200"), body_of(read)
    assert b"bob-account" not in read.data


def test_a_platform_admin_is_refused_a_members_account_file(web):
    """The platform qualification is not ownership of the account tree."""
    _, bob_file = seeded_accounts(web)

    served = web.get("/api/file?path=" + quote(bob_file),
                     token=web.login("root"), tenant=False)
    assert not served.status.startswith("200"), body_of(served)
    assert b"bob-account" not in served.data


def test_search_never_returns_a_colleagues_account_file(web):
    """The container is not a panel surface, so neither account tree is walked.

    ``users/`` is refused rather than filtered (its only entries are private
    trees), which also stops the search's descent at the container -- the same
    reason it is not offered in a listing.
    """
    seed_account(web, web.alice, "memory", "needle_alice.md", content=b"needle alice")
    seed_account(web, web.bob, "memory", "needle_bob.md", content=b"needle bob")

    response = web.get("/api/workspace/search?q=needle&agent=" + AGENT,
                       token=web.login("alice"))
    assert response.status.startswith("200"), body_of(response)
    assert "needle_bob" not in body_of(response)
    assert "needle_alice" not in body_of(response)
