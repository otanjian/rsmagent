# encoding:utf-8
"""The tool-name contract every model provider enforces.

Field report: asking the agent "你可以使用的mcp工具？" returned

    {"error": {"message": "Invalid 'tools[18].function.name': string does not
     match pattern '^[a-zA-Z0-9_-]+$'"}} (Status: 400)

An external connection's tool names are composed from three sources, and two of
them are not ours to constrain: the action id (``tools.read``, containing a dot),
the connection id, and the *remote* tool name published by a third-party MCP
server. The failure mode is what makes this worth a contract rather than a
per-name fix: the provider rejects **the whole request** over one bad name, so a
single unusable tool name costs the entire turn — the model cannot even answer
"hello", and the error points at an index rather than at the tool.

So the guarantee is split in two, and both halves are asserted here:

* :func:`agent.tools.mcp.external.tool_name` never composes a name that cannot be
  sent, including for remote names that contain illegal characters or are too
  long to carry;
* :func:`agent.protocol.agent_stream.build_tools_schema` refuses to send one
  anyway, so a future naming mistake costs one tool instead of the turn.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from agent.protocol.agent_stream import build_tools_schema
from agent.tools.base_tool import MAX_TOOL_NAME, is_wire_safe_name
from agent.tools.external.external_tool import TOOL_PREFIX, _external_tool_name
from agent.tools.mcp.external import REMOTE_DIGEST_LEN, tool_name

#: Spelled out literally rather than imported, so changing the contract has to
#: be a deliberate edit here as well.
WIRE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

CONNECTION_ID = "conn_cN-lhbMpQlwpiL7k"


def _has_identity_digest(name: str) -> bool:
    """Whether the segment carries the identity digest appended by the composer."""
    return bool(re.search(r"_[0-9a-f]{%d}$" % REMOTE_DIGEST_LEN, name))


def _wire_ok(composed: str) -> bool:
    """The name the *provider* sees, which is the prefixed one."""
    return bool(WIRE_NAME_RE.match(_external_tool_name(composed)))


class _Tool:
    """The minimum a tool needs to reach a provider's tool list."""

    def __init__(self, name: str, params: Dict[str, Any] = None):
        self.name = name
        self.description = "probe"
        self.params = params or {"type": "object", "properties": {}}

    def get_json_schema(self):
        return None


# -- the composer ----------------------------------------------------------- #

def test_a_connection_level_name_has_no_dot():
    """动作 id 自带点号（``tools.read``），它过去直接进了名字。"""
    name = tool_name(action="tools.read", connection_id=CONNECTION_ID)

    assert _wire_ok(name)
    assert "." not in name
    assert name == "mcp_tools_read_%s" % CONNECTION_ID


def test_a_discovered_name_carries_its_connection_and_its_remote_tool():
    """名字先回答「哪个连接」，再回答「哪个工具」。"""
    name = tool_name(action="tools.read", connection_id=CONNECTION_ID,
                     remote_name="search_knowledge")

    assert name == "mcp_tools_read_%s_search_knowledge" % CONNECTION_ID
    assert _wire_ok(name)


def test_a_remote_name_needing_no_rewrite_is_kept_verbatim():
    """普通情况读起来还是服务端写的样子，摘要只在必需时出现。"""
    for remote in ("search_knowledge", "wiki-read-page", "v2.search"):
        name = tool_name(action="tools.read", connection_id=CONNECTION_ID,
                         remote_name=remote)
        assert _wire_ok(name)
        if remote == "v2.search":
            assert REMOTE_DIGEST_LEN and _has_identity_digest(name)
        else:
            assert name.endswith("_" + remote)


def test_every_character_a_server_may_publish_survives_the_composer():
    """远端名是第三方输入：空格、斜杠、点号、非 ASCII 都可能出现。"""
    remote_names = [
        "tools.read", "weird name/with, junk", "知识库检索", "a" * 120,
        "a" * 500, "..", "/", "\n", "emoji 🔧", "Mixed.Case/Name",
    ]

    names = [tool_name(action="tools.read", connection_id=CONNECTION_ID,
                       remote_name=remote) for remote in remote_names]

    assert all(names), "每个远端工具都要有名字，否则等于把它藏起来"
    for name in names:
        assert _wire_ok(name), name


def test_two_names_that_rewrite_to_the_same_string_stay_distinct():
    """抹平会丢信息，所以段里带上原名的摘要——两条都必须可寻址。"""
    first = tool_name(action="tools.read", connection_id=CONNECTION_ID,
                      remote_name="tools.read")
    second = tool_name(action="tools.read", connection_id=CONNECTION_ID,
                       remote_name="tools-read")

    assert first != second
    assert _has_identity_digest(first), first
    # 原名本身合法时保持原样，所以它不需要摘要。
    assert second.endswith("_tools-read")


def test_the_composer_is_independent_of_who_calls_it_first():
    """名字只由输入决定：同一远端名任何时候都得到同一个名字。"""
    calls = {tool_name(action="tools.read", connection_id=CONNECTION_ID,
                       remote_name="weird name") for _ in range(5)}

    assert len(calls) == 1


def test_the_budget_is_measured_against_the_model_visible_name():
    """模型看到的是加了前缀的名字，预算按它算。"""
    name = tool_name(action="resources.read", connection_id="conn_" + "z" * 22,
                     remote_name="e" * 128)

    assert _wire_ok(name), len(_external_tool_name(name))
    assert len(_external_tool_name(name)) <= MAX_TOOL_NAME


def test_an_empty_action_or_connection_has_no_name():
    """空段不该组合出一个「看起来像真绑定」的名字（``mcp_tools_read_``）。"""
    assert tool_name(action="tools.read", connection_id="") == ""
    assert tool_name(action="", connection_id=CONNECTION_ID) == ""


def test_is_wire_safe_name_states_the_contract():
    assert is_wire_safe_name("web_search")
    assert is_wire_safe_name("external_mcp_tools_read_x")
    assert is_wire_safe_name("-")
    assert not is_wire_safe_name("web.search")
    assert not is_wire_safe_name("web search")
    assert not is_wire_safe_name("知识库")
    assert not is_wire_safe_name("")
    assert not is_wire_safe_name(None)
    assert not is_wire_safe_name("a" * (MAX_TOOL_NAME + 1))
    assert is_wire_safe_name("a" * MAX_TOOL_NAME)


# -- the boundary gate ------------------------------------------------------ #

def test_one_bad_name_costs_one_tool_not_the_turn():
    """最后一道闸门：坏名字被拦下，好的照旧发出。"""
    tools = [_Tool("web_search"), _Tool("todo.read"), _Tool("bash"),
             _Tool("a" * 200)]

    schema = build_tools_schema(tools)

    assert [entry["name"] for entry in schema] == ["web_search", "bash"]


def test_the_gate_keeps_a_name_that_only_looks_suspicious():
    """闸门只按契约判断，不做别的取舍。"""
    tools = [_Tool("mcp_tools_read_conn_cN-lhbMpQlwpiL7k_search_knowledge"),
             _Tool("-_-")]

    schema = build_tools_schema(tools)

    assert len(schema) == 2


def test_the_schema_still_prefers_a_runtime_schema_when_one_exists():
    """闸门不改既有行为：动态 schema 仍然优先。"""

    class Dynamic(_Tool):
        def get_json_schema(self):
            return {"parameters": {"type": "object",
                                   "properties": {"text": {"type": "string"}}}}

    schema = build_tools_schema([Dynamic("echo")])

    assert schema[0]["input_schema"]["properties"] == {"text": {"type": "string"}}


def test_an_empty_tool_list_is_still_an_empty_list():
    assert build_tools_schema([]) == []
    assert build_tools_schema(None) == []


def test_the_prefix_is_what_makes_the_name_model_visible():
    """前缀不是装饰：被拒绝的是加了前缀之后的名字。"""
    composed = tool_name(action="tools.read", connection_id=CONNECTION_ID,
                         remote_name="echo")

    assert composed.startswith("mcp_")
    assert _external_tool_name(composed) == TOOL_PREFIX + composed
