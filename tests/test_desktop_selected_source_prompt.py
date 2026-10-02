"""The chosen desktop input must reach the model, not only the tool instance."""

from types import SimpleNamespace
import json
import re

import pytest

from agent.protocol.agent import Agent
from agent.tools.client_files.client_files import ClientFiles
from bridge.agent_bridge import _attach_desktop_context_to_tools


REFERENCE = {"binding_id": "bind_selected", "workspace_id": "ws_selected",
             "grant_version": 1}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr("common.i18n.get_language", lambda: "zh")
    monkeypatch.setattr(ClientFiles, "is_available", lambda self: True)
    monkeypatch.setattr("agent.prompt.builder.conf", lambda: {"knowledge": False})
    value = Agent(system_prompt="BASE", tools=[ClientFiles()],
                  workspace_dir=str(tmp_path), skip_context_files=True)
    _attach_desktop_context_to_tools(value, {"desktop_context": dict(REFERENCE)})
    return value


def test_selected_input_reaches_prompt_without_changing_server_cwd(agent):
    before = agent.effective_cwd()
    prompt = agent.get_full_system_prompt()
    assert "本轮已选择客户端本地目录" in prompt
    assert '"op":"list"' in prompt
    assert "materialize" in prompt
    assert "不能据此判断所选目录没有文件" in prompt
    assert agent.effective_cwd() == before
    assert "bind_selected" not in prompt
    assert "ws_selected" not in prompt


def test_clear_selection_removes_both_prompt_and_tool_context(agent):
    assert "本轮已选择客户端本地目录" in agent.get_full_system_prompt()
    _attach_desktop_context_to_tools(agent, {})
    assert "本轮已选择客户端本地目录" not in agent.get_full_system_prompt()
    assert agent.tools[0].desktop_context is None


def test_replacing_selection_updates_only_the_current_reference(agent):
    replacement = dict(REFERENCE, binding_id="bind_new", workspace_id="ws_new")
    _attach_desktop_context_to_tools(agent, {"desktop_context": replacement})
    assert agent.desktop_context == replacement
    assert agent.tools[0].desktop_context == replacement
    assert "本轮已选择客户端本地目录" in agent.get_full_system_prompt()


@pytest.mark.parametrize("missing", ["disabled", "not_installed"])
def test_unavailable_tool_does_not_turn_selected_input_into_server_input(agent, monkeypatch, missing):
    if missing == "disabled":
        monkeypatch.setattr(ClientFiles, "is_available", lambda self: False)
    else:
        agent.tools = []
        _attach_desktop_context_to_tools(agent, {"desktop_context": dict(REFERENCE)})
    prompt = agent.get_full_system_prompt()
    assert "本轮已选择客户端本地目录" in prompt
    assert "当前不可用" in prompt
    assert '"op":"list"' not in prompt
    assert "不能据此判断所选目录没有文件" in prompt


def test_prompt_rebuild_failure_keeps_current_source(agent, monkeypatch):
    agent.skip_context_files = False
    def broken(_):
        raise OSError("workspace unavailable")
    monkeypatch.setattr("agent.prompt.load_context_files", broken)
    prompt = agent.get_full_system_prompt()
    assert prompt.startswith("BASE")
    assert "本轮已选择客户端本地目录" in prompt
    _attach_desktop_context_to_tools(agent, None)
    assert "本轮已选择客户端本地目录" not in agent.get_full_system_prompt()


def test_english_selection_guidance(agent, monkeypatch):
    monkeypatch.setattr("common.i18n.get_language", lambda: "en")
    prompt = agent.get_full_system_prompt()
    assert "selected a desktop directory for this turn" in prompt
    assert '"op":"list"' in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_root_listing_example_satisfies_device_contract(agent, monkeypatch, language):
    from auth.desktop_contracts import validate_command_frame
    monkeypatch.setattr("common.i18n.get_language", lambda: language)
    example = re.search(r"client_files\((\{[^}]+\})\)", agent.get_full_system_prompt())
    args = json.loads(example.group(1))
    op = args.pop("op")
    assert validate_command_frame({
        "v": 1, "type": "command", "request_id": "test", "connection_epoch": "test",
        "binding_id": "test", "workspace_id": "test", "grant_version": 1,
        "op": op, "params": args, "deadline": 1, "params_sha256": "test",
    }) == []


def test_execution_project_keeps_its_existing_local_tool_routing(agent):
    agent.workspace_scope = "local"
    agent.project_dir = "/client/project"
    assert "本轮已选择客户端本地目录" not in agent.get_full_system_prompt()


def test_guidance_is_sent_on_the_real_model_request(agent, monkeypatch):
    from agent.protocol.agent_stream import AgentStreamExecutor

    requests = []
    class Captured(Exception):
        pass
    def capture(request):
        requests.append(request)
        raise Captured()

    executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
    executor.model = SimpleNamespace(model="capture", call_stream=capture,
                                     use_fallback=lambda: False)
    executor.agent = None
    executor.messages = []
    executor.tools = {}
    executor.system_prompt = agent.get_full_system_prompt()
    monkeypatch.setattr(executor, "_validate_and_fix_messages", lambda: None)
    monkeypatch.setattr(executor, "_prepare_messages", lambda: [])
    monkeypatch.setattr(executor, "_identify_complete_turns", lambda: [])
    monkeypatch.setattr(executor, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_is_thinking_enabled", lambda: False)
    monkeypatch.setattr("agent.protocol.agent_stream.time.sleep", lambda _: None)
    with pytest.raises(Captured):
        executor._call_llm_stream(retry_on_empty=False, max_retries=0)
    assert "本轮已选择客户端本地目录" in requests[0].system
