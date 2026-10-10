# encoding:utf-8
"""Old attachments keep resolving over the real Wire, and are re-authorized.

change ``unify-private-agent-personal-workspace`` (task 3.3/4.4). The unit tests
(``test_legacy_attachment_map.py``) pin the decision table; what only the WSGI app
answers is whether ``GET /api/file`` (a) consults the table *before* its gates and
(b) still applies every gate to the mapped path. A table that could widen access
would be a privilege-escalation tool, so the owner/non-owner/platform-admin cases
are the point of this file.
"""

import json
import os
from urllib.parse import quote

import pytest

from agent.workspace import legacy_attachment_map as lam
from tests._helpers import WebAppHarness

AGENT = "shared-agent"


@pytest.fixture()
def web(tmp_path, monkeypatch):
    harness = WebAppHarness(tmp_path)
    harness.workspace = os.path.join(harness.shared_root, "agents", AGENT)
    harness.write_roster([
        {"id": AGENT, "name": "Shared", "workspace": harness.workspace},
    ])
    harness.add_agent(AGENT)
    harness.alice = harness.member("alice", ["member"])
    harness.bob = harness.member("bob", ["member"])

    map_path = os.path.join(str(tmp_path), "state", "migration",
                            "legacy_attachment_map.json")
    monkeypatch.setenv(lam.ENV_VAR, map_path)
    lam.reset_cache()
    harness.map_path = map_path
    yield harness
    harness.close()


def write_map(web, mapping):
    os.makedirs(os.path.dirname(web.map_path), exist_ok=True)
    with open(web.map_path, "w") as handle:
        json.dump({"version": 1, "mapping": mapping}, handle)
    lam.reset_cache()


def write(path, content=b"payload"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def body_of(response):
    return response.data.decode("utf-8", "replace")


def test_the_owner_reads_a_moved_file_through_its_old_path(web):
    new_path = write(os.path.join(web.workspace, "user", web.alice,
                                  "outputs", "report.pdf"),
                     content=b"alice-report")
    old_path = os.path.join(web.workspace, "outputs", "report.pdf")
    write_map(web, {os.path.realpath(old_path): new_path})

    response = web.get("/api/file?path=" + quote(old_path),
                       token=web.login("alice"), tenant=False)

    assert response.status.startswith("200"), body_of(response)
    assert response.data == b"alice-report"


def test_a_non_owner_is_refused_through_the_old_path(web):
    new_path = write(os.path.join(web.workspace, "user", web.alice,
                                  "outputs", "report.pdf"),
                     content=b"alice-secret")
    old_path = os.path.join(web.workspace, "outputs", "report.pdf")
    write_map(web, {os.path.realpath(old_path): new_path})

    response = web.get("/api/file?path=" + quote(old_path),
                       token=web.login("bob"), tenant=False)

    assert not response.status.startswith("200"), body_of(response)
    assert b"alice-secret" not in response.data


def test_a_platform_admin_is_still_refused_a_members_moved_file(web):
    new_path = write(os.path.join(web.workspace, "user", web.alice,
                                  "outputs", "report.pdf"),
                     content=b"alice-secret")
    old_path = os.path.join(web.workspace, "outputs", "report.pdf")
    write_map(web, {os.path.realpath(old_path): new_path})

    response = web.get("/api/file?path=" + quote(old_path),
                       token=web.login("root"), tenant=False)

    assert not response.status.startswith("200"), body_of(response)
    assert b"alice-secret" not in response.data


def test_a_reused_old_path_is_a_conflict_not_the_new_content(web):
    old_path = write(os.path.join(web.workspace, "outputs", "report.pdf"),
                     content=b"someone-else-now")
    new_path = write(os.path.join(web.workspace, "user", web.alice,
                                  "outputs", "report.pdf"),
                     content=b"alice-original")
    write_map(web, {os.path.realpath(old_path): new_path})

    response = web.get("/api/file?path=" + quote(old_path),
                       token=web.login("alice"), tenant=False)

    assert response.status.startswith("409"), body_of(response)
    assert b"someone-else-now" not in response.data
    assert b"alice-original" not in response.data


def test_a_broken_target_is_reported_missing(web):
    old_path = os.path.join(web.workspace, "outputs", "report.pdf")
    write_map(web, {os.path.realpath(old_path): os.path.join(
        web.workspace, "user", web.alice, "outputs", "gone.pdf")})

    response = web.get("/api/file?path=" + quote(old_path),
                       token=web.login("alice"), tenant=False)

    assert response.status.startswith("404"), body_of(response)


def test_a_path_with_no_mapping_still_serves_normally(web):
    live = write(os.path.join(web.workspace, "user", web.alice,
                              "uploads", "live.txt"), content=b"live")
    write_map(web, {})

    response = web.get("/api/file?path=" + quote(live),
                       token=web.login("alice"), tenant=False)

    assert response.status.startswith("200"), body_of(response)
    assert response.data == b"live"


def test_the_preview_url_mounts_the_migrated_directory(web):
    """A regenerated preview must mount where the page *is*, not the old dir."""
    from urllib.parse import unquote

    from channel.web.fork.runtime import _build_preview_url, _decode_dir_token

    new_dir = os.path.join(web.workspace, "user", web.alice, "outputs")
    new_path = write(os.path.join(new_dir, "page.html"),
                     content=b"<html>old-page</html>")
    old_path = os.path.join(web.workspace, "outputs", "page.html")
    write_map(web, {os.path.realpath(old_path): new_path})

    url = _build_preview_url(old_path)
    token, _, name = url[len("/preview/"):].partition("/")

    assert _decode_dir_token(token) == os.path.realpath(new_dir)
    assert unquote(name) == "page.html"


def test_the_owner_reads_a_migrated_preview_and_others_cannot(web):
    from channel.web.fork.runtime import _build_preview_url

    new_path = write(os.path.join(web.workspace, "user", web.alice,
                                  "outputs", "page.html"),
                     content=b"<html>old-page</html>")
    old_path = os.path.join(web.workspace, "outputs", "page.html")
    write_map(web, {os.path.realpath(old_path): new_path})
    url = _build_preview_url(old_path)

    owner = web.get(url, token=web.login("alice"), tenant=False)
    assert owner.status.startswith("200"), body_of(owner)
    assert b"old-page" in owner.data

    other = web.get(url, token=web.login("bob"), tenant=False)
    assert not other.status.startswith("200"), body_of(other)


def test_a_migrated_attachment_still_gets_an_artifact_card(web, monkeypatch):
    """History replay must not drop a card just because the file moved."""
    from agent.protocol import artifact as artifact_module
    from channel.web.fork.runtime import _artifacts_from_steps

    new_path = write(os.path.join(web.workspace, "user", web.alice,
                                  "outputs", "old.bin"), content=b"data")
    old_path = os.path.join(web.workspace, "outputs", "old.bin")
    write_map(web, {os.path.realpath(old_path): new_path})
    monkeypatch.setattr(artifact_module, "get_workspace_root",
                        lambda *args, **kwargs: web.workspace)
    steps = [{"type": "tool", "name": "write", "is_error": False,
              "result": json.dumps({"abs_path": old_path})}]

    cards = _artifacts_from_steps(steps)

    assert cards, "the card must survive the move"
    assert os.path.realpath(cards[0]["abs_path"]) == os.path.realpath(new_path)
    assert "path=" + quote(os.path.realpath(new_path)) in cards[0]["raw_url"]


