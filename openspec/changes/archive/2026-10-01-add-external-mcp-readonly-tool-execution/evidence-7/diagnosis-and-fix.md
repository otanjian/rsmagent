# 模型面工具名：现场诊断与修复证据（第 8 组）

## 1. 现场报告

2026-09-27 19:03，在智能体「税务健康体检（`tax-health-check-test15`）」里问
「你可以使用的mcp工具？」，界面返回一整个 JSON 错误块而不是回答：

```
Invalid 'tools[18].function.name': string does not match pattern
'^[a-zA-Z0-9_-]+$'. (Status: 400)
```

拒绝的是**整次请求**：模型没有产出任何内容，错误只给一个下标 `18`，不告诉运维是谁的名字有问题。

## 2. 根因（改动前既有缺陷，不是本组引入）

`run.log` 里同一句话的两次尝试都失败，且失败发生在授权链打通前后：

| 时间 | 投放的工具数 | 结果 |
| --- | --- | --- |
| 16:46:30 | 4（连接级） | 同一 `tools[18]` 400 |
| 19:03:37 | 6（连接级，`read_execute` 已开放） | 同一 `tools[18]` 400 |

16:46 的进程运行的是**本组改动之前**的代码（本组第一次编辑测试文件是 16:57），所以这是既有缺陷；授权链打通后工具真正进入模型面，它才变得无法回避。

名字由三段构成，其中两段不受我们控制：

```
external_ + mcp + . + tools.read + . + conn_cN-lhbMpQlwpiL7k + . + search_knowledge
                    ↑ 动作标识自带点号     ↑ 生成标识                ↑ 第三方服务器发布
```

`. ` 分隔符与动作标识里的 `.` 一起进入名字。用现场同一提供方实测（**最小验证**，两组各一次请求）：

```
external_mcp.tools.read.conn_x.ask       -> 400
external_mcp_tools_read_conn_x_ask       -> 200
```

长度也是硬约束，且只修字符集**必然**撞上它：

```
len=100 -> 200      len=130 -> 400  string too long. Expected a maximum length of 128
上界组合：external_(9) + mcp_tools_read(14) + 分隔符 + conn_cN-lhbMpQlwpiL7k(22) + 分隔符 + 远端名(≤128) ≈ 175
```

## 3. 修复

见 `design.md` 的 D9 与 `tasks.md` 第 8 组。要点：分隔符 `_`；每段规范化；**只在丢信息时**追加原始远端名的 SHA-1 前 12 位（名字只由输入决定、与列表顺序无关）；长度预算按模型可见名（含 `external_` 前缀）计算；无法命名时跳过而不是发出去；`build_tools_schema()` 再兜一道，坏名字只损失一个工具。

## 4. 现场复验（同一份现场数据）

### 4.1 投影与实发 — `live_tool_name_wire_contract.txt`

```
.venv/bin/python openspec/changes/add-external-mcp-readonly-tool-execution/evidence-7/live_tool_name_wire_contract.py
```

| 检查 | 结果 |
| --- | --- |
| `external_tools_for()` 投放 | 15 个工具，15/15 符合契约 |
| 名字里出现的契约外字符 | `[]`（空集） |
| `build_tools_schema()`（真正发出的那层） | 15 条，0 条不合规，0 条被闸门拦下 |
| 把该 schema 原样发给现场提供方 | **200**（19:03 的同一批工具定义是 400） |

第 3 步是唯一的外部调用（`max_tokens=1`）。

### 4.2 真实会话 — `browser/chat-mcp-tools-19-18.png`

重启现场进程后，在**同一个会话**里问同一句话（`_mcp_name` 格式的名字出现在 `run.log` 的
`external tools synced` 里）：

```
19:18:27 [Routing] → 🤖 税务健康体检(tax-health-check-test15)
19:18:27 [Agent] external tools synced: +[6 个连接级名字]
19:18:28 [Agent] external tools synced: +[12 个远端工具名字]
19:18:28 🔧 external_mcp_tools_list_conn_8r2lE23PRk0Sd_Wb, external_mcp_tools_list_conn_cN-lhbMpQlwpiL7k
19:18:28   ❌ external_mcp_tools_list_conn_8r2lE23PRk0Sd_Wb (0.02s): target_not_allowed
19:18:28   ✅ external_mcp_tools_list_conn_cN-lhbMpQlwpiL7k (0.02s): {"tools": ["add_document", "ask", ...]}
19:18:30 [Agent] 🏁 Done (2 turns)
```

`DEEPSEEK] API error`、`invalid_request_error`、`does not match pattern` 在新进程日志中 **0 次**。

模型最终回答（`browser/chat-mcp-tools-19-18.png`，同文）：

- 列出 `weknora-rsmagent` 的 12 个只读知识库工具及各自用途；
- 说明另一个连接 `OneAgent HTTP MCP` 当前不可用，并引用真实拒绝原因
  `target_not_allowed: host api.example.com resolves to an address that is not allowed (198.18.0.26)`。

第二条与本组无关，是既有策略拒绝（该主机不在部署白名单内），如实出现在回答里说明工具面已经真实可用了。

## 5. 回归

```
.venv/bin/python -m pytest tests/ -q -p no:randomly -k "external or mcp or tool_schema or agent_stream or self_authorized or parallel_tool"
→ 2 failed, 1045 passed, 1 skipped

.venv/bin/python -m pytest tests/test_agent_*.py tests/test_tool_*.py tests/test_mcp_*.py \
  tests/test_self_authorized_tools.py tests/test_parallel_tool_calls.py \
  tests/test_tenant_admin_tool_execution.py tests/test_scheduler_tool_dispatch.py \
  tests/test_todo_tool_identity.py -q -p no:randomly
→ 516 passed
```

第一条的两处失败是**变更前既有缺陷**（`test_plain_member_cannot_manage_tenant_connections`、
`test_a_tenant_catalogue_read_needs_the_read_permission`），与工具命名无关，判定过程记录在
`evidence-5/regression.md` 第 4 节（在变更前 commit `8aec12f1` 上同样失败）。两条都与目录读取权限
`external.connections.read` 有关，不经过 `may_execute`。

本组新增/改写的用例：

```
.venv/bin/python -m pytest tests/test_tool_name_wire_contract.py tests/test_external_mcp_adapter.py \
  tests/test_external_connection_assignment_runtime.py tests/test_external_authorization.py \
  tests/test_external_scheduler_boundary.py tests/test_external_dispatch_gate_surfaces.py \
  tests/test_external_test_binding.py -q -p no:randomly
→ 164 passed
```

## 6. 未实施项

- **提供方差异未处理**：上限取现场提供方的 128。OpenAI 文档的工具名上限是 64，若将来接入该厂商，
  这些名字（当前最长 57 字符）会重新越界。契约常量处已写明这一点，但没有为它预留截断策略——
  那需要一个按厂商的预算，而当前部署只有一个厂商。
- **遗留 MCP 集成（`agent/tools/mcp/mcp_tool.py`）未纳入**：它的名字是 `<tool_name_prefix><远端名>`，
  前缀由 `_PREFIX_RE` 校验（允许点号），远端名未校验字符集。现场该路径的前缀是 `erp_`，未触发；
  同一类缺陷仍可能在别的部署上出现，按「不顺手扩大范围」单独提出。
- **同名兜底的两条跳过分支**（连接内重名、跨连接同名）没有现场触发条件，只有单元测试覆盖。
