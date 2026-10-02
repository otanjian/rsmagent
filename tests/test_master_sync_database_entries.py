"""Master additions through the served database routes, with real scoped storage."""
import json
from pathlib import Path

from common.runtime_identity import RuntimeIdentity, use_identity


def body(response):
    return json.loads(response.data)


def setup_members(web_app):
    app = web_app()
    app.add_agent("agent-a")
    app.role("history", ["chat.use", "agent.use", "agent.read", "history.read", "skill.read", "skill.edit"],
             [("agent", "agent:agent-a", "use"), ("agent", "agent:agent-a", "read")])
    alice = app.member("alice", ["history"])
    bob = app.member("bob", ["history"])
    return app, alice, bob


def test_timeline_and_cursor_keep_owner_and_tenant(web_app):
    from agent.registry import get_agent_registry
    from agent.memory import get_conversation_store

    app, alice, bob = setup_members(web_app)
    store = get_conversation_store(get_agent_registry().get("agent-a").workspace)
    identity = RuntimeIdentity(user_id=alice, tenant_id=app.tenant_id, agent_id="agent-a")
    with use_identity(identity):
        store.append_messages("session-timeline", [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "recent question"},
        ], channel_type="web")
    url = "/api/history/user_messages?agent_id=agent-a&session_id=session-timeline"
    result = app.get(url, app.login("alice"))
    assert result.status == "200 OK", result.data
    assert [m["preview"] for m in body(result)["messages"]] == ["earlier question", "recent question"]
    assert body(app.get(url, app.login("bob"))).get("messages", []) == []
    assert app.get(url).status.startswith("401")
    foreign = app.stack.other_tenant(code="other", agent="foreign-agent")
    response = app.get(url, app.login("alice"), headers={"X-Tenant-ID": foreign["tenant_id"]})
    assert response.status.startswith(("403", "404")), response.data
    # The new bounded history read returns the selected turn without crossing owner.
    result = app.get("/api/history?agent_id=agent-a&session_id=session-timeline&page=1&until_seq=0", app.login("alice"))
    assert result.status == "200 OK" and body(result)["status"] == "success"
    assert "earlier question" in [m["content"] for m in body(result)["messages"]]
    with use_identity(RuntimeIdentity(user_id=bob, tenant_id=app.tenant_id, agent_id="agent-a")):
        assert store.latest_seq("session-timeline") is None
        assert store.list_user_messages("session-timeline")["messages"] == []
        store.clear_context("session-timeline")
    with use_identity(identity):
        assert store.get_context_start_seq("session-timeline") == 0


def upload(app, token, *, query="", name="merge-skill"):
    boundary = "merge-skill-boundary"
    content = f"---\nname: {name}\ndescription: isolated merge acceptance\n---\nTest-only skill.\n"
    data = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="SKILL.md"\r\n\r\n'
            + content + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="paths"\r\n\r\n{name}/SKILL.md\r\n--{boundary}--\r\n').encode()
    return app.post("/api/skills/upload" + query, data.decode(), token,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                             "Content-Length": str(len(data))})


def test_skill_upload_confirm_delete_use_the_tenant_library(web_app):
    app, alice, bob = setup_members(web_app)
    admin = app.login("root")
    assert upload(app, app.login("alice")).status.startswith("403")
    response = upload(app, admin)
    assert response.status == "200 OK", response.data
    preview = body(response)
    assert preview["status"] == "success", preview
    root = Path(app.shared_root) / "skills"
    assert not (root / "merge-skill").exists(), "preview never installs"
    token = preview["token"]
    # A different administrator can manage the same tenant but cannot claim the preview.
    app.member("other-admin", ["tenant_admin"])
    denied = app.post("/api/skills", {"action": "confirm", "token": token}, app.login("other-admin"))
    assert denied.status.startswith("403"), denied.data
    installed = app.post("/api/skills", {"action": "confirm", "token": token}, admin)
    assert body(installed)["installed"] == ["merge-skill"], installed.data
    assert (root / "merge-skill/SKILL.md").read_text().endswith("Test-only skill.\n")
    rows = body(app.get("/api/skills", admin))["skills"]
    skill = next(row for row in rows if row["name"] == "merge-skill")
    deleted = app.post("/api/skills", {"action": "delete", "resource_id": skill["resource_id"]}, admin)
    assert body(deleted)["status"] == "success", deleted.data
    assert not (root / "merge-skill").exists()


def test_private_skill_upload_query_cannot_write_shared_or_another_owner(web_app):
    from agent.registry import get_agent_registry

    app, alice, bob = setup_members(web_app)
    app.private_agent(alice, "alice-private")
    private_root = Path(get_agent_registry().get("alice-private").workspace) / "skills"
    token = app.login("alice")
    # No own skills directory: the Agent consumes shared skills; owner != tenant admin.
    assert upload(app, token, query="?agent_id=alice-private").status.startswith("403")
    private_root.mkdir(parents=True)
    preview_response = upload(app, token, query="?agent_id=alice-private")
    assert preview_response.status == "200 OK", preview_response.data
    preview = body(preview_response)
    assert preview["status"] == "success", preview
    assert upload(app, app.login("bob"), query="?agent_id=alice-private").status.startswith("403")
    result = app.post("/api/skills", {"action": "confirm", "token": preview["token"], "agent_id": "alice-private"}, token)
    assert body(result)["installed"] == ["merge-skill"], result.data
    assert (private_root / "merge-skill/SKILL.md").exists()
    assert not (Path(app.shared_root) / "skills/merge-skill").exists()


def test_clear_team_revalidates_every_owner_before_writing(web_app):
    from agent.registry import get_agent_registry
    from agent.memory import get_conversation_store
    from agent.workspace import session_prefs

    app, alice, bob = setup_members(web_app)
    app.add_agent("agent-b")
    admin = app.login("root")
    root = app.user_id("root")
    sid = "session_team_clear"
    stores = {aid: get_conversation_store(get_agent_registry().get(aid).workspace)
              for aid in ("agent-a", "agent-b")}
    def identity(uid, aid="agent-a"):
        return use_identity(RuntimeIdentity(user_id=uid, tenant_id=app.tenant_id, agent_id=aid))
    with identity(root):
        for store in stores.values():
            store.append_messages(sid, [{"role": "user", "content": "owned transcript"}], channel_type="web")
        session_prefs.set_prefs(sid, agent_id="agent-a", members=["agent-b"])
    result = app.post(f"/api/sessions/{sid}/clear_context", {"agent_id": "agent-a"}, admin)
    assert result.status == "200 OK", result.data
    with identity(root):
        assert all(store.get_context_start_seq(sid) == 1 for store in stores.values())
    # Another same-named transcript must not become visible through the new read.
    with identity(bob):
        assert stores["agent-b"].get_context_start_seq(sid) == 0
    blocked_sid = "session_team_blocked"
    with identity(root):
        stores["agent-a"].append_messages(blocked_sid, [{"role": "user", "content": "own"}], channel_type="web")
        session_prefs.set_prefs(blocked_sid, agent_id="agent-a", members=["agent-b"])
    with identity(bob, "agent-b"):
        stores["agent-b"].append_messages(blocked_sid, [{"role": "user", "content": "private"}], channel_type="web")
    refused = app.post(f"/api/sessions/{blocked_sid}/clear_context", {"agent_id": "agent-a"}, admin)
    assert refused.status.startswith(("403", "404")), refused.data
    with identity(root):
        assert stores["agent-a"].get_context_start_seq(blocked_sid) == 0
    with identity(bob, "agent-b"):
        assert stores["agent-b"].get_context_start_seq(blocked_sid) == 0
