# encoding:utf-8
"""The onboarding copy round-trips over the real wire.

change ``improve-agent-chat-onboarding``. The Agent detail pane rebuilds its
form from ``GET /api/agents`` after every save (``console.js``:
``loadAgentCatalog()`` -> ``renderAgentDetail()``), so a write the management
projection does not echo is a field that appears to vanish the moment it is
saved — which is the "输入后点保存，内容又不见了" report this file guards
against. The service layer is covered by ``tests/test_agent_admin.py``; what
only the wire can answer is that the *route* accepts the fields, the
*management* read returns them (the detail pane) and the *workbench* read
returns them (the welcome hero), with omit-preserves / empty-clears in between.

Two harness details are load-bearing, borrowed from the sibling boundary file:
the Agent has to be **created through the app** (the runtime registry that
resolves an update is the one the create registered into), and the id is
derived from ``tmp_path`` so no two runs share a store entry.
"""

import pytest

from tests._helpers import WebAppHarness


@pytest.fixture
def web(tmp_path):
    harness = WebAppHarness(tmp_path / "instance")
    yield harness
    harness.close()


@pytest.fixture
def agent_id(tmp_path):
    stem = f"{tmp_path.parent.name}-{tmp_path.name}".replace("_", "-").lower()
    return "onboarding-" + stem.removeprefix("pytest-")


@pytest.fixture
def agent(web, agent_id):
    """An ordinary Agent this tenant owns, made the way the console makes one."""
    response = web.post("/api/agents",
                        {"action": "create", "id": agent_id, "name": "Onboarding Agent"},
                        token=web.login("root"))
    assert web.json(response)["status"] == "success", response.data
    return agent_id


def update(web, agent_id, **fields):
    body = {"action": "update", "id": agent_id, **fields}
    return web.json(web.post("/api/agents", body, token=web.login("root")))


def management_row(web, agent_id):
    """The row the console's Agent detail pane renders from."""
    body = web.json(web.get("/api/agents", token=web.login("root")))
    assert body["status"] == "success", body
    return {row["id"]: row for row in body["agents"]}[agent_id]


def workbench_row(web, agent_id):
    """The row the empty-chat welcome hero renders from."""
    body = web.json(web.get("/api/agents?view=workbench", token=web.login("root")))
    assert body["status"] == "success", body
    return {row["id"]: row for row in body["agents"]}[agent_id]


def test_the_saved_copy_survives_the_reload_the_form_does(web, agent):
    usage = "描述需求或提供会议纪要 → 补齐关键信息 → 确认需求与方案"
    questions = [
        "只有一个初步想法，能带我梳理需求吗？",
        "分析客户需求，需要先准备哪些资料？",
    ]

    result = update(web, agent, usage_hint=usage, suggested_questions=questions)
    assert result["status"] == "success", result

    # The read the form rebuilds itself from, and the read the welcome page
    # renders from, must both carry what was just saved.
    saved = management_row(web, agent)
    assert saved["usage_hint"] == usage
    assert saved["suggested_questions"] == questions
    hero = workbench_row(web, agent)
    assert hero["usage_hint"] == usage
    assert hero["suggested_questions"] == questions


def test_an_update_that_omits_the_copy_keeps_it(web, agent):
    assert update(web, agent, usage_hint="保留我",
                  suggested_questions=["保留我"])["status"] == "success"

    # A console page that predates the two fields, or any caller that only
    # means to rename, sends neither: absent must preserve.
    assert update(web, agent, name="Onboarding Renamed")["status"] == "success"

    row = management_row(web, agent)
    assert row["name"] == "Onboarding Renamed"
    assert row["usage_hint"] == "保留我"
    assert row["suggested_questions"] == ["保留我"]


def test_an_explicit_empty_clears_the_copy(web, agent):
    assert update(web, agent, usage_hint="清掉我",
                  suggested_questions=["清掉我"])["status"] == "success"

    assert update(web, agent, usage_hint="",
                  suggested_questions=[])["status"] == "success"

    # The roster omits an empty optional value — the house convention that keeps
    # a legacy profile from being rewritten with empty keys — so the cleared
    # state reads back as "absent", which is what the form renders as blank.
    assert not management_row(web, agent).get("usage_hint")
    assert not management_row(web, agent).get("suggested_questions")
    # The welcome hero normalises absence to an explicit empty, because it must
    # tell "not configured" from "nothing to show".
    assert workbench_row(web, agent)["usage_hint"] == ""
    assert workbench_row(web, agent)["suggested_questions"] == []


def test_a_created_agent_can_carry_the_copy_from_the_start(web, agent_id):
    usage = "说出任务 → 补充时间与要求 → 确认下一步安排"
    questions = ["事情太多，能帮我梳理今天的优先级吗？"]

    response = web.post("/api/agents", {
        "action": "create", "id": agent_id, "name": "Onboarding Agent",
        "greeting": "我帮你梳理日常工作。",
        "usage_hint": usage, "suggested_questions": questions,
    }, token=web.login("root"))
    assert web.json(response)["status"] == "success", response.data

    row = management_row(web, agent_id)
    assert row["greeting"] == "我帮你梳理日常工作。"
    assert row["usage_hint"] == usage
    assert row["suggested_questions"] == questions


def test_an_over_long_hint_is_refused_without_touching_the_stored_one(web, agent):
    assert update(web, agent, usage_hint="原来的说明")["status"] == "success"

    body = update(web, agent, usage_hint="很" * 201)
    assert body["status"] == "error", body
    assert management_row(web, agent)["usage_hint"] == "原来的说明"


def test_a_fifth_question_and_a_slash_command_are_refused(web, agent):
    assert update(web, agent,
                  suggested_questions=["一", "二", "三", "四"])["status"] == "success"

    too_many = update(web, agent, suggested_questions=["一", "二", "三", "四", "五"])
    assert too_many["status"] == "error", too_many
    # A question is ordinary prose: it must never become a command entry point.
    command = update(web, agent, suggested_questions=["/help"])
    assert command["status"] == "error", command
    assert management_row(web, agent)["suggested_questions"] == ["一", "二", "三", "四"]
