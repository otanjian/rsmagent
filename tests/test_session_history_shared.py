"""Shared history must paginate in SQL without widening account/tenant scope."""
from types import SimpleNamespace

import pytest

from agent.memory.conversation_store import ConversationStore
from channel.web.fork.runtime import _list_sessions_across_agents


@pytest.fixture
def shared(tmp_path, monkeypatch):
    import agent.memory
    import agent.registry
    import common.state_dir
    from agent.workspace import project_store, session_prefs
    from channel.web import web_channel

    profiles = [SimpleNamespace(id=aid, workspace=aid, name=aid, avatar="", enabled=True)
                for aid in ("main", "b", "foreign", "disabled")]
    stores = {p.id: ConversationStore(tmp_path / "shared.db", "" if p.id == "main" else p.id)
              for p in profiles}
    monkeypatch.setattr(agent.registry, "get_agent_registry", lambda: SimpleNamespace(
        list=lambda **kw: profiles[:-1], get_addressed=lambda aid: profiles[0]))
    monkeypatch.setattr(agent.memory, "get_conversation_store", lambda ws: stores[ws])
    monkeypatch.setattr(web_channel, "_tenant_ids_for_context", lambda ctx: ["main", "b", "disabled"])
    monkeypatch.setattr(session_prefs, "members_index", lambda: {})
    monkeypatch.setattr(common.state_dir, "state_root_str", lambda: str(tmp_path))
    project_data = {"sessions": {}, "meta": {}, "order": []}
    loads = []
    monkeypatch.setattr(project_store, "_load", lambda: loads.append(1) or project_data)
    ctx = SimpleNamespace(user_id="alice", tenant_id="team")

    def seed(sid, aid="", owner="alice", tenant="team", active=1, count=1,
             pinned=0, archived=0, title=None, channel="web"):
        conn = stores["main"]._connect()
        with conn:
            conn.execute("INSERT INTO sessions (agent_id, session_id, owner, tenant_id,"
                         " channel_type, title, created_at, last_active, msg_count, pinned, archived)"
                         " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                         (aid, sid, owner, tenant, channel, title or sid, active, count, pinned, archived))
        conn.close()

    return SimpleNamespace(stores=stores, seed=seed, ctx=ctx, data=project_data,
                           loads=loads, root=tmp_path)


def test_shared_paging_scopes_all_dimensions_and_never_reads_full_id_lists(shared, monkeypatch):
    env = shared
    env.seed("pin", pinned=1)
    env.seed("recent", aid="b", active=50)
    env.seed("old", active=2)
    for sid, extra in (("foreign", {"aid": "foreign"}), ("disabled", {"aid": "disabled"}),
                       ("owner", {"owner": "bob"}), ("tenant", {"tenant": "other"}),
                       ("legacy", {"owner": ""}), ("im", {"channel": "weixin"}),
                       ("archived", {"archived": 1})):
        env.seed(sid, active=999, **extra)

    def forbidden(*args, **kwargs):
        pytest.fail("shared history must not fall back to per-agent/id scans")
    monkeypatch.setattr(ConversationStore, "list_sessions", forbidden)
    monkeypatch.setattr(ConversationStore, "list_session_ids", forbidden)
    first = _list_sessions_across_agents(1, 2, env.ctx)
    assert [s["session_id"] for s in first["sessions"]] == ["pin", "recent"]
    assert [s["agent"]["id"] for s in first["sessions"]] == ["main", "b"]
    assert first["total"] == 3 and first["has_more"]
    assert len(env.loads) == 1
    last = _list_sessions_across_agents(2, 2, env.ctx)
    assert [s["session_id"] for s in last["sessions"]] == ["old"]
    assert last["total"] == 3 and not last["has_more"]
    assert _list_sessions_across_agents(3, 2, env.ctx)["sessions"] == []
    assert _list_sessions_across_agents(1, 2, env.ctx, archived=True)["total"] == 1


def test_dedup_chooses_fullest_copy_before_paging_even_outside_agent_prefix(shared):
    env = shared
    env.seed("shared", count=1, active=100, pinned=1)
    env.seed("shared", aid="b", count=9, active=1)
    env.seed("recent", aid="b", active=90)
    first = _list_sessions_across_agents(1, 1, env.ctx)
    second = _list_sessions_across_agents(2, 1, env.ctx)
    assert first["total"] == second["total"] == 2
    assert first["sessions"][0]["session_id"] == "recent"
    assert second["sessions"][0]["agent"]["id"] == "b"
    assert second["sessions"][0]["msg_count"] == 9
    assert not second["sessions"][0]["pinned"]


def test_search_project_statistics_and_directory_checks_use_one_snapshot(shared, monkeypatch):
    from agent.workspace import project_store
    env = shared
    folder = env.root / "project"
    folder.mkdir()
    env.seed("a", title="Budget 100%_", active=20)
    env.seed("b", aid="b", title="other")
    env.seed("default", title="other")
    env.seed("hidden", owner="bob")
    env.data["sessions"] = {
        "main::a": {"path": str(folder)}, "b::b": {"path": str(folder)},
        "main::hidden": {"path": str(env.root)},
        "foreign::ignored": {"path": "/never-check-this"},
    }
    env.data["meta"] = {str(folder): {"display_name": "Renamed project"}}
    checked = []
    isdir = project_store.os.path.isdir
    monkeypatch.setattr(project_store.os.path, "isdir", lambda p: checked.append(p) or isdir(p))
    result = _list_sessions_across_agents(1, 50, env.ctx, q="100%_")
    assert result["total"] == 1 and result["space_count"] == 2
    assert result["group_mode"] == "project"
    assert result["sessions"][0]["project"]["name"] == "Renamed project"
    assert checked.count(str(folder)) == 1
    assert "/never-check-this" not in checked
    assert len(env.loads) == 1


def test_empty_visible_agents_never_widen_scope(shared, monkeypatch):
    from channel.web import web_channel
    shared.seed("private")
    monkeypatch.setattr(web_channel, "_tenant_ids_for_context", lambda ctx: [])
    assert _list_sessions_across_agents(1, 50, shared.ctx)["total"] == 0
    assert shared.stores["main"].list_sessions_for_agents([], user_id="alice")["total"] == 0


def test_shared_legacy_dates_still_sort_by_time(shared):
    shared.seed("new", active="2026-01-01 00:00:00")
    shared.seed("old", aid="b", active="2020-01-01 00:00:00")
    shared.seed("middle", active=1700000000)
    result = _list_sessions_across_agents(1, 50, shared.ctx)
    assert [s["session_id"] for s in result["sessions"]] == ["new", "middle", "old"]
