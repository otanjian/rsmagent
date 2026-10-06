"""History search queries the authorized corpus before deduplication/paging."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import web

from agent.memory.conversation_store import ConversationStore
from channel.web import web_channel


def _seed(store, rows):
    conn = store._connect()
    try:
        conn.executemany(
            """INSERT INTO sessions
               (session_id, title, owner, channel_type, created_at, last_active,
                msg_count, pinned) VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            [(sid, title, owner, channel, active, count, pinned)
             for sid, title, owner, channel, active, count, pinned in rows],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


@pytest.mark.parametrize("q, expected", [
    ("  erPnext  ", ["erp"]),
    ("采购", ["cn"]),
    ("%", ["percent"]),
    ("_", ["underscore"]),
    ("'", ["quote"]),
    ("\\", ["backslash"]),
    ("' OR 1=1 --", []),
    ("正文关键词", []),
])
def test_title_search_is_literal_and_excludes_message_bodies(store, q, expected):
    titles = [
        ("erp", "ERPNext疑问"), ("cn", "系统采购现状"),
        ("percent", "完成100%"), ("underscore", "file_name"),
        ("quote", "O'Reilly"), ("backslash", "folder\\file"),
        ("body", "其他会话"),
    ]
    _seed(store, [(sid, title, "u1", "web", 20, 1, 0) for sid, title in titles])
    conn = store._connect()
    try:
        conn.execute(
            """INSERT INTO messages (session_id, seq, role, content, created_at, owner)
               VALUES ('body', 1, 'user', '正文关键词', 1, 'u1')""",
        )
        conn.commit()
    finally:
        conn.close()
    result = store.list_sessions(channel_type="web", user_id="u1", q=q)
    assert [s["session_id"] for s in result["sessions"]] == expected
    assert result["total"] == len(expected)
    assert result["has_more"] is False


def test_title_search_filters_before_paging_and_keeps_owner_and_channel(store):
    _seed(store, [
        ("latest", "unrelated", "u1", "web", 999, 1, 0),
        ("pin", "MATCH pinned", "u1", "web", 1, 1, 1),
        ("newer", "match newer", "u1", "web", 50, 1, 0),
        ("older", "match older", "u1", "web", 20, 1, 0),
        ("other-owner", "match", "u2", "web", 1000, 1, 1),
        ("legacy-owner", "match", "", "web", 1000, 1, 1),
        ("other-channel", "match", "u1", "weixin", 1000, 1, 1),
    ])
    first = store.list_sessions(channel_type="web", user_id="u1", q="match", page_size=2)
    second = store.list_sessions(channel_type="web", user_id="u1", q="match", page_size=2, page=2)
    assert [s["session_id"] for s in first["sessions"]] == ["pin", "newer"]
    assert [s["session_id"] for s in second["sessions"]] == ["older"]
    assert first["total"] == second["total"] == 3
    assert first["has_more"] is True
    assert second["has_more"] is False
    beyond = store.list_sessions(channel_type="web", user_id="u1", q="match", page=3, page_size=2)
    assert beyond["sessions"] == [] and beyond["total"] == 3 and not beyond["has_more"]


def test_omitted_and_whitespace_query_preserve_old_results(store):
    _seed(store, [("s1", "旧会话", "", "web", 10, 2, 0)])
    expected = store.list_sessions(channel_type="web")
    assert store.list_sessions(channel_type="web", q="") == expected
    assert store.list_sessions(channel_type="web", q=" \t\n ") == expected
    assert store.list_sessions(channel_type="web", q="旧会话")["sessions"] == expected["sessions"]
    assert set(expected) == {"sessions", "total", "page", "page_size", "has_more"}


def test_query_length_counts_unicode_characters_without_truncation(store):
    title = "搜" * 99 + "😀"
    _seed(store, [("s1", title, "", "web", 1, 1, 0)])
    assert store.list_sessions(q=" " + title + " ")["total"] == 1
    with pytest.raises(ValueError, match="at most 100"):
        store.list_sessions(q=title + "多")


@pytest.fixture
def agent_environment(tmp_path, monkeypatch):
    import agent.memory
    import agent.registry
    import auth.service
    import common.state_dir
    from agent.workspace import project_store, session_prefs

    profiles = {
        name: SimpleNamespace(id=name, name=name, avatar=None,
                              workspace=name, enabled=name != "disabled")
        for name in ("a", "b", "foreign", "disabled")
    }
    stores = {name: ConversationStore(tmp_path / (name + ".db")) for name in profiles}
    opened = []

    def get_store(workspace):
        opened.append(workspace)
        return stores[workspace]

    registry = SimpleNamespace(
        list=lambda include_disabled=False: [
            profile for profile in profiles.values() if include_disabled or profile.enabled
        ],
        get=lambda agent_id, **kwargs: profiles[agent_id or "a"],
    )
    monkeypatch.setattr(agent.registry, "get_agent_registry", lambda: registry)
    monkeypatch.setattr(agent.memory, "get_conversation_store", get_store)
    monkeypatch.setattr(auth.service, "get_identity_service", lambda: SimpleNamespace(
        tenant_agent_ids=lambda tenant: ["a", "b", "disabled"] if tenant == "tenant-1" else [],
    ))
    monkeypatch.setattr(session_prefs, "members_index", lambda: {})
    monkeypatch.setattr(project_store, "get_project_map", lambda agent_id: {})
    monkeypatch.setattr(project_store, "get_order", lambda: [])
    monkeypatch.setattr(common.state_dir, "state_root_str", lambda: str(tmp_path))
    from auth.runtime import RequestContext
    ctx = RequestContext(
        user_id="u1", username="u1", display_name="u1",
        is_platform_admin=False, must_change_password=False,
        tenant_id="tenant-1", membership={"id": "m1"},
        permissions={"history.read"}, is_tenant_admin=False,
    )
    return SimpleNamespace(stores=stores, opened=opened, ctx=ctx)


def test_search_merges_all_batches_then_deduplicates_and_pages(agent_environment):
    env = agent_environment
    # More than one batch; the duplicate would be missing from early page candidates.
    _seed(env.stores["a"], [
        (f"a{i}", f"needle {i}", "u1", "web", 1000 - i, 1, 0)
        for i in range(502)
    ] + [("duplicate", "needle shared", "u1", "web", 1, 1, 0)])
    _seed(env.stores["b"], [
        ("duplicate", "NEEDLE shared", "u1", "web", 2, 9, 1),
        ("hidden-user", "needle hidden", "u2", "web", 9000, 1, 1),
    ])
    for agent in ("foreign", "disabled"):
        _seed(env.stores[agent], [(agent, "needle hidden", "u1", "web", 9000, 1, 1)])
    first = web_channel._list_sessions_across_agents(1, 2, env.ctx, q=" needle ")
    assert [s["session_id"] for s in first["sessions"]] == ["duplicate", "a0"]
    assert first["sessions"][0]["agent"]["id"] == "b"
    assert first["total"] == 503 and first["has_more"] is True
    last = web_channel._list_sessions_across_agents(252, 2, env.ctx, q="needle")
    assert [s["session_id"] for s in last["sessions"]] == ["a501"]
    assert last["total"] == 503 and last["has_more"] is False
    assert set(env.opened) == {"a", "b"}
    assert first["group_mode"] == "time" and first["space_count"] == 1


def _request(monkeypatch, ctx, **params):
    monkeypatch.setattr(web_channel, "_db_scope", lambda: nullcontext(ctx))
    app = web.application(("/api/sessions", "SessionsHandler"), vars(web_channel), autoreload=False)
    response = app.request("/api/sessions?" + urlencode(params))
    return response, json.loads(response.data.decode("utf-8"))


def test_route_echoes_search_and_empty_query_is_backwards_compatible(agent_environment, monkeypatch):
    env = agent_environment
    _seed(env.stores["a"], [("s1", "标题搜索", "u1", "web", 1, 1, 0)])
    response, result = _request(monkeypatch, env.ctx, scope="all", q=" 标题 ")
    assert response.status == "200 OK"
    assert result["status"] == "success" and result["query"] == "标题"
    assert result["total"] == 1
    _, original = _request(monkeypatch, env.ctx, scope="all")
    _, cleared = _request(monkeypatch, env.ctx, scope="all", q=" \t ")
    assert original == cleared and "query" not in cleared


def test_route_rejects_long_query_before_reading_any_store(agent_environment, monkeypatch):
    env = agent_environment
    response, result = _request(monkeypatch, env.ctx, scope="all", q="搜" * 101)
    assert response.status == "400 Bad Request"
    assert result["status"] == "error" and result["code"] == "invalid_query"
    assert "sessions" not in result and env.opened == []


def test_search_route_requires_history_permission(agent_environment, monkeypatch):
    env = agent_environment
    env.ctx.permissions = set()
    response, result = _request(monkeypatch, env.ctx, scope="all", q="secret")
    assert response.status == "403 Forbidden"
    assert result["status"] == "error"
    assert "sessions" not in result and env.opened == []


def test_single_agent_search_scopes_project_metadata_to_user(agent_environment, monkeypatch):
    from agent.workspace import project_store

    env = agent_environment
    _seed(env.stores["a"], [
        ("mine", "needle", "u1", "web", 1, 1, 0),
        ("legacy", "needle", "", "web", 2, 1, 0),
    ])
    # No ``_get_workspace_root`` stub: the single-Agent branch must open the
    # addressed Agent's own store, exactly like the merged ``scope=all`` branch.
    monkeypatch.setattr(project_store, "get_project_map", lambda agent_id: {"mine": "/work/mine"})
    monkeypatch.setattr(project_store, "display_name_for", lambda path: "My project")
    _, result = _request(monkeypatch, env.ctx, agent_id="a", q="needle")
    assert result["query"] == "needle" and result["total"] == 1
    assert result["space_count"] == 1
    assert result["sessions"][0]["session_id"] == "mine"
    assert result["sessions"][0]["project"]["name"] == "My project"


def test_merged_page_reads_only_the_needed_prefix(agent_environment, monkeypatch):
    env = agent_environment
    _seed(env.stores["a"], [
        (f"a{i}", f"title {i}", "u1", "web", 2000 - i, 1, 0)
        for i in range(80)
    ])
    sizes = []
    original = ConversationStore.list_sessions

    def wrapped(self, *args, **kwargs):
        sizes.append(kwargs.get("page_size", args[2] if len(args) > 2 else 50))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ConversationStore, "list_sessions", wrapped)
    first = web_channel._list_sessions_across_agents(1, 8, env.ctx)
    assert first["total"] == 80
    assert [s["session_id"] for s in first["sessions"]] == [f"a{i}" for i in range(8)]
    assert sizes and max(sizes) == 8
