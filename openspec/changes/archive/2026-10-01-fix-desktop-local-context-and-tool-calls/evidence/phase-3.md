# 阶段三证据 — 模型协议（任务 3.1 / 3.2）

Change: `fix-desktop-local-context-and-tool-calls` · 环境见 `phase-1.md`

## 3.1 先把正常路径钉住，再谈异常

修复顺序是"先确认标准协议没问题"，而不是先清洗正文：

- 请求侧形状经核对（`phase-1.md` §1.2）：`models/deepseek/deepseek_bot.py:284-286` 在工具集非空时写
  `tools` + `tool_choice`（默认 `auto`）。**未**为本次异常改动工具定义、`tool_choice`、thinking 参数或模型选择。
- 正常结构化调用在流接缝上验证：`tests/test_tool_protocol_guard.py::test_structured_tool_call_still_flows`
  （`tool_calls` 流入执行，`content` 与 `_stop` 不受影响）；
  `test_a_real_call_beside_a_text_marker_is_not_discarded`（结构化批次是唯一执行依据）。
- 未偷偷切换模型，也没有把正文解释成命令。

## 3.2 窄范围检查

落点：`agent/protocol/tool_protocol.py`（分类）+ `agent/protocol/agent_stream.py:1937-1952`（触发）。

**分类规则**（`is_text_tool_call_anomaly`，`tool_protocol.py:77`）：

1. 先剥离围栏代码块、行内代码与引用行（`strip_examples`），解释/引用格式的回复保持为普通文本；
2. 仅当**同时**出现 DSML 标记的 `tool_calls` 与 `invoke name=` 两个封装标签才判定异常 —— 只提到 "DSML" 或只出现单个标签都不是；
3. 分类器不解析、不执行封装；没有通用 DSML/协议解释器。

**触发位置**（`agent_stream.py:1946`）：`if not tool_calls and is_text_tool_call_anomaly(full_content)`。

- 在**分片拼接之后**判定（`test_wrapper_split_across_chunks_is_seen_whole`）；
- 在**写入助手历史之前**抛出（`test_failure_happens_before_success_history_is_written`：`executor.messages` 前后一致），
  因此不会产生成功持久化，也不会进入工具执行（执行次数为零）；
- 抛在重试/回退接缝**之后**、网络重试循环之外（`ToolProtocolError` 非瞬时错误，`tool_protocol.py:49-58`）；
- 由 `run_stream` 既有 `except` 转为既有 `error` 事件（`agent_stream.py:1179-1182`），复用未被改写的失败流程。

## 回归（已执行）

`tests/test_tool_protocol_guard.py`：12 例通过。

| 覆盖 | 用例 |
|---|---|
| 命中 | `test_execution_wrapper_is_detected` |
| 不误伤：只提 DSML / 单标签 / 围栏 / 行内代码 / 引用行 | `test_bare_dsml_word_is_not_enough`、`test_wrapper_only_needs_one_of_the_two_tags_to_stay_text`、`test_fenced_code_example_is_ignored`、`test_inline_code_and_blockquote_examples_are_ignored`、`test_explaining_the_format_does_not_fail_the_turn` |
| 流接缝 | `test_structured_tool_call_still_flows`、`test_text_wrapper_without_a_call_fails_the_turn`、`test_wrapper_split_across_chunks_is_seen_whole`、`test_failure_happens_before_success_history_is_written`、`test_a_real_call_beside_a_text_marker_is_not_discarded` |
| 剥离工具本身 | `test_strip_examples_keeps_prose` |

## 未完成

- 规格 §「排查实际模型出口」要求"真实标准调用冒烟与模拟异常测试分别记录"。模拟一侧已由上表覆盖；
  **真实出口冒烟未执行**（没有可用的真实 provider 凭据/原始 SSE），因此不据此宣称 provider 兼容性，
  也不把责任归于模型或代理。见 `verification.md`。
