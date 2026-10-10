# encoding:utf-8
"""``POST /api/agents`` release-governance actions over the real WSGI app.

Change ``add-shared-agent-release-governance``. The service layer is covered by
``tests/test_shared_agent_release_governance.py``; what only the wire can answer
is who reaches the route, that the deployment switch is enforced, and that the
console can drive the request -> approve -> publish flow.
"""

import pytest

from tests._helpers import WebAppHarness


@pytest.fixture()
def web(tmp_path):
    harness = WebAppHarness(
        tmp_path, settings={"shared_agent_release_governance": True})
    yield harness
    harness.close()


def _release(web, action, body, token):
    return web.json(web.post("/api/agents", {"action": action, **body},
                             token=token))


def test_member_cannot_request_a_release(web):
    web.add_agent("shared-agent")
    web.member("member1", ["member"])

    body = _release(web, "request_release", {"id": "shared-agent"},
                    web.login("member1"))

    assert body["code"] == "forbidden", body


def test_request_approve_publish_flow(web):
    web.add_agent("shared-agent")
    web.member("admin2", ["tenant_admin"])
    root_token = web.login("root")

    requested = _release(web, "request_release", {"id": "shared-agent"}, root_token)
    assert requested["status"] == "success", requested
    approval_id = requested["result"]["id"]
    assert requested["result"]["content_hash"]

    # Separation of duties: a second control user approves.
    web.service.decide_approval(
        actor_user_id=web.user_id("admin2"), tenant_id=web.tenant_id,
        approval_id=approval_id, approve=True)

    released = _release(web, "release",
                        {"id": "shared-agent", "approval_id": approval_id},
                        root_token)
    assert released["status"] == "success", released
    assert released["result"]["release_state"] == "released"
    assert released["result"]["version"] == 1

    history = _release(web, "list_releases", {"id": "shared-agent"}, root_token)
    assert history["status"] == "success", history
    assert len(history["result"]["releases"]) == 1
    assert history["result"]["release_status"]["release_state"] == "released"


def test_release_without_approval_is_refused(web):
    web.add_agent("shared-agent")

    body = _release(web, "release",
                    {"id": "shared-agent", "approval_id": "apr_missing"},
                    web.login("root"))

    assert body["code"] in ("approval_unknown", "approval_mismatch"), body


def test_an_unknown_agent_is_not_found(web):
    web.add_agent("shared-agent")

    response = web.post("/api/agents",
                        {"action": "request_release", "id": "ghost-agent"},
                        token=web.login("root"))
    body = web.json(response)

    assert response.status.startswith("404"), body
    assert body.get("code") == "not_found", body


def test_a_missing_id_is_a_bad_request(web):
    web.add_agent("shared-agent")

    body = _release(web, "request_release", {}, web.login("root"))

    assert body["code"] == "invalid_agent_id", body


def test_switch_off_refuses_the_release_actions(tmp_path):
    harness = WebAppHarness(tmp_path)  # default: governance off
    try:
        harness.add_agent("shared-agent")
        body = _release(harness, "release",
                        {"id": "shared-agent", "approval_id": "apr_x"},
                        harness.login("root"))
        assert body["code"] == "release_governance_disabled", body
    finally:
        harness.close()
