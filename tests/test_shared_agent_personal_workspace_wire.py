# encoding:utf-8
"""The personal default directory, driven through the real WSGI app (tasks 3.1/3.2).

change ``use-personal-workspace-for-shared-agents``. The unit suite
(``test_shared_agent_personal_workspace.py``) proves the resolver, the session
cwd and the prompt are consistent; what only the real app can answer is whether
every *route* agrees with them -- the picker hint the console reads, the
directory the panel materializes, and the owner rule that decides who may
download what the member generated there.

The fixture is the product's shape: Alice and Bob are two members of one tenant
sharing one Agent that lives inside the tenant's shared root.
"""

import os
from urllib.parse import quote

import pytest

from tests._helpers import WebAppHarness

AGENT = "shared-agent"


@pytest.fixture()
def web(tmp_path):
    harness = WebAppHarness(tmp_path)
    harness.workspace = os.path.join(harness.shared_root, "agents", AGENT)
    harness.write_roster([
        {"id": AGENT, "name": "Shared", "workspace": harness.workspace},
    ])
    harness.add_agent(AGENT)
    harness.alice = harness.member("alice", ["member"])
    harness.bob = harness.member("bob", ["member"])
    yield harness
    harness.close()


def projects(web, token, session="s-wire"):
    """The picker state one member sees for their own session."""
    response = web.get(f"/api/projects?session={session}&agent={AGENT}",
                       token=token)
    assert response.status.startswith("200"), response.data
    return WebAppHarness.json(response)


def personal(web, user_id):
    return os.path.realpath(os.path.join(web.workspace, "user", user_id))


def test_each_member_is_shown_their_own_default_directory(web):
    alice = projects(web, web.login("alice"))
    bob = projects(web, web.login("bob"))

    assert os.path.realpath(alice["default_workspace"]) == personal(web, web.alice)
    assert os.path.realpath(bob["default_workspace"]) == personal(web, web.bob)
    assert alice["default_workspace"] != bob["default_workspace"]


def test_the_hint_is_read_only(web):
    """Showing the directory must not create it."""
    projects(web, web.login("alice"))

    assert not os.path.exists(personal(web, web.alice))


def test_the_personal_directory_is_not_registered_as_a_project(web):
    alice = web.login("alice")
    response = web.post("/api/workspace/user-dir",
                        {"session": "s-wire", "agent": AGENT}, token=alice)
    assert response.status.startswith("200"), response.data
    assert os.path.isdir(personal(web, web.alice))

    state = projects(web, alice)
    assert state["current"] is None
    assert os.path.realpath(state["default_workspace"]) == personal(web, web.alice)
    assert personal(web, web.alice) not in {
        os.path.realpath(entry.get("path", "")) for entry in state["recents"]
    }


def test_a_file_generated_there_is_downloadable_by_its_owner_only(web):
    """The member's own folder, addressed exactly as the runtime resolves it."""
    alice, bob = web.login("alice"), web.login("bob")
    target = os.path.join(personal(web, web.alice), "output", "报告.txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("alice-private")

    own = web.get("/api/file?path=" + quote(target), token=alice, tenant=False)
    assert own.status.startswith("200"), own.data
    assert own.data.decode("utf-8") == "alice-private"

    other = web.get("/api/file?path=" + quote(target), token=bob, tenant=False)
    assert not other.status.startswith("200"), other.data
    assert b"alice-private" not in other.data


def test_an_unsafe_user_container_is_refused_and_nothing_is_written(web):
    """A symlinked ``user`` entry must not be presented, let alone written."""
    elsewhere = os.path.join(str(web.root), "elsewhere")
    os.makedirs(web.workspace, exist_ok=True)
    os.makedirs(elsewhere, exist_ok=True)
    os.symlink(elsewhere, os.path.join(web.workspace, "user"))

    alice = web.login("alice")
    hint = web.get(f"/api/projects?session=s-unsafe&agent={AGENT}", token=alice)
    body = WebAppHarness.json(hint)
    assert body.get("status") != "success", body
    assert "default_workspace" not in body

    materialize = web.post("/api/workspace/user-dir",
                           {"session": "s-unsafe", "agent": AGENT},
                           token=alice)
    payload = WebAppHarness.json(materialize)
    assert payload.get("status") == "error", payload
    assert os.listdir(elsewhere) == []
