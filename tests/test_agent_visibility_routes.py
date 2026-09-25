# encoding:utf-8
"""``POST /api/agents {"action": "set_visibility"}`` over the real WSGI app.

change ``show-and-toggle-agent-visibility``. The service layer is covered by
``tests/test_agent_visibility_toggle.py``; what only the wire can answer is who
reaches the route at all, and that another tenant's Agent is "not found" rather
than "forbidden" (a 403 would confirm the id exists somewhere the caller cannot
see).
"""

import pytest

from tests._helpers import WebAppHarness


@pytest.fixture()
def web(tmp_path):
    harness = WebAppHarness(tmp_path)
    yield harness
    harness.close()


def test_the_owner_shares_their_own_agent_over_the_wire(web):
    web.add_agent("shared-agent")
    rock = web.member("rock", ["member"])
    web.private_agent(rock, "rock-agent")

    response = web.post("/api/agents",
                        {"action": "set_visibility", "id": "rock-agent",
                         "visibility": "tenant"},
                        token=web.login("rock"))
    body = web.json(response)

    assert body["status"] == "success", body
    assert body["result"]["visibility"] == "tenant"
    assert body["result"]["changed"] is True
    assert web.service.get_agent_binding("rock-agent")["private_owner_user_id"] is None


def test_a_fellow_member_cannot_share_it(web):
    web.add_agent("shared-agent")
    rock = web.member("rock", ["member"])
    jane = web.member("jane", ["member"])
    web.private_agent(rock, "rock-agent")

    response = web.post("/api/agents",
                        {"action": "set_visibility", "id": "rock-agent",
                         "visibility": "tenant"},
                        token=web.login("jane"))
    body = web.json(response)

    assert body["code"] == "forbidden", body
    assert web.service.get_agent_binding("rock-agent")["private_owner_user_id"] == rock


def test_an_admin_restores_it_to_a_named_member(web):
    web.add_agent("shared-agent")
    rock = web.member("rock", ["member"])
    web.private_agent(rock, "rock-agent")

    token = web.login("root")
    assert web.json(web.post("/api/agents",
                             {"action": "set_visibility", "id": "rock-agent",
                              "visibility": "tenant"},
                             token=token))["status"] == "success"
    assert web.json(web.post("/api/agents",
                             {"action": "set_visibility", "id": "rock-agent",
                              "visibility": "private", "owner_user_id": rock},
                             token=token))["status"] == "success"
    assert web.service.get_agent_binding("rock-agent")["private_owner_user_id"] == rock


def test_restoring_the_tenant_default_reports_its_own_reason(web):
    web.add_agent("shared-agent")
    token = web.login("root")
    assert web.json(web.post("/api/agents",
                             {"action": "set_default", "id": "shared-agent"},
                             token=token))["status"] == "success"

    response = web.post("/api/agents",
                        {"action": "set_visibility", "id": "shared-agent",
                         "visibility": "private", "owner_user_id": web.admin_id},
                        token=token)
    body = web.json(response)

    # The refusal keeps its own code so the console can say *why* rather than
    # reporting a generic failure.
    assert body["code"] == "agent_is_tenant_default", body
    assert web.service.tenant_default_agent_id(web.tenant_id) == "shared-agent"


def test_another_tenants_agent_is_not_found(web):
    other = web.service.create_tenant(
        actor_user_id=web.admin_id, code="beta", name="Beta",
        shared_root=str(tmp_path_shared(web)), admin_username="broot",
        admin_display="BRoot", admin_password=web.ADMIN_PASSWORD,
        recent_password=web.ADMIN_PASSWORD)["id"]
    web.bind_agent_to_tenant("beta-agent", other)

    response = web.post("/api/agents",
                        {"action": "set_visibility", "id": "beta-agent",
                         "visibility": "tenant"},
                        token=web.login("root"))
    body = web.json(response)

    assert response.status.startswith("404"), body
    # Nothing about the foreign object leaks — neither its owner nor its
    # existence beyond "not here".
    assert body.get("code") == "not_found", body
    assert "private_owner_user_id" not in body
    assert web.service.get_agent_binding("beta-agent")["tenant_id"] == other


def tmp_path_shared(web):
    import os
    return os.path.join(web.root, "tenants", "beta")


def test_an_unknown_visibility_is_a_bad_request(web):
    web.add_agent("shared-agent")
    response = web.post("/api/agents",
                        {"action": "set_visibility", "id": "shared-agent",
                         "visibility": "public"},
                        token=web.login("root"))
    body = web.json(response)

    assert body["code"] == "bad_request", body
    assert web.service.get_agent_binding("shared-agent")["private_owner_user_id"] is None


def test_a_missing_id_never_falls_back_to_the_tenant_default(web):
    web.add_agent("shared-agent")
    response = web.post("/api/agents",
                        {"action": "set_visibility", "visibility": "private",
                         "owner_user_id": web.admin_id},
                        token=web.login("root"))
    body = web.json(response)

    assert body["code"] == "invalid_agent_id", body
    assert web.service.get_agent_binding("shared-agent")["private_owner_user_id"] is None


def test_sharing_does_not_reload_the_agent_runtime(web):
    """The binding lives in ``identity.db``, not in the roster.

    A conversion changes no Agent's configuration, so dropping cached sessions
    (which is what a reload costs) would evict warm conversations for nothing.
    """
    from unittest.mock import patch

    web.add_agent("shared-agent")
    rock = web.member("rock", ["member"])
    web.private_agent(rock, "rock-agent")

    with patch("channel.web.web_channel._reload_agent_runtime") as reloader:
        response = web.post("/api/agents",
                            {"action": "set_visibility", "id": "rock-agent",
                             "visibility": "tenant"},
                            token=web.login("rock"))
    assert web.json(response)["status"] == "success"
    reloader.assert_not_called()
