## 1. 动作与风险归档（`mcp-connection-integration`）

- [x] 1.1 确认 `integrations/external/adapters/mcp.py` 的 `ACTION_TOOLS_READ = "tools.read"` 仍在 `ACTIONS` 内且**不**进入 `WRITE_ACTIONS`；`tools.call` 的写语义与审批要求保持不变（`tests/test_external_mcp_adapter.py::test_read_only_tool_calls_are_a_read_action_separate_from_the_write_one`）。
- [x] 1.2 改写 `integrations/external/risk.py` 的 `(mcp, tools.read)` 条目注释：删除「放行依据是连接声明」，如实写明本地不再有逐工具判定，风险由部署开关与连接分配承担；`(mcp, tools.call)` 条目不变。
- [x] 1.3 `_offered_actions()` 与 `_action_states()` 去掉逐工具声明条件：`read_execute` 开放即报告并投放 `tools.read`；`tools.call` 仍只在 `write_execute` 开放时投放。
- [x] 1.4 适配器测试：`read_execute` 开放时读动作被投放且可执行、关闭时被拒；`tools.call` 在 `write_execute` 关闭时仍被拒；`write_actions` 集合不因读动作扩大。

## 2. 移除逐工具声明（`mcp-connection-integration`）

- [x] 2.1 MCP `config_keys` 不含 `read_only_tools`，也没有服务于它的规范化分支（`registry.py` 的 MCP 字段集合为 `transport/url/auth/header_name/command/args/env_keys/oauth_provider/tool_name_prefix`）。
- [x] 2.2 测试：提交含 `read_only_tools` 的 MCP 配置被拒绝（`unknown_field`，指名字段），且不写入部分配置（`test_a_per_tool_declaration_is_not_a_configuration_field`）。
- [x] 2.3 测试：遗留行（`config_json` 里仍有该键）在读取、调用、以及按不含该键的完整配置再次保存三条路径上都不报错，且行为与字段不存在时一致（`test_a_stored_row_that_still_carries_the_removed_key_keeps_working`）。
- [x] 2.4 核对 `scripts/seed_external_connections.py`、`integrations/external/migration.py` 的 legacy 导入白名单与连接种子：均不涉及该键，无需改动。

## 3. 发现工具一律按读动作投放（`mcp-connection-integration`）

- [x] 3.1 `_discovered_bindings()` 不再分流：每个候选都以 `tools.read`（`write=False`）绑定，绑定名与 metadata 形状不变；`tools.call` 只保留为连接级动作。
- [x] 3.2 `discovered_tools_offered()` 简化为「本部署能否投放读动作」（`rows` 参数保留但不再用于判断）；`_mcp_tool_provider()` 不再计算任何声明集合。
- [x] 3.3 在 `agent/tools/mcp/external.py` 增加 `remembered_tool_names(*, tenant_id, connection_id, version)`，按连接当前版本返回已发现的远端工具名，未知时返回 `None`（不做 I/O）。
- [x] 3.4 `_invoke_tools_read()` 的边界：名字必须在 3.3 的发现结果内；不在其中或发现结果未知时以 `tool_not_published` 拒绝，且**不**建立到远端的连接、不转发任何参数；`_read_only_names()` 与 `tool_not_read_only` 已不存在。
- [x] 3.5 测试：连接发布的工具可经读动作调用且不要求审批；发现结果之外的远端名被拒且断言远端未被触达（`initialized == 0`、`calls == []`）；发现结果未知（未 memo 或版本不匹配）时同样被拒。
- [x] 3.6 测试：远端 `readOnlyHint` 为真或为假都不改变工具的动作归属、风险等级与可用性。
- [x] 3.7 保留并复核参数封装用例：发现工具的远端名由绑定注入、模型参数进入 `arguments`；模型参数字段名恰为 `tool` 时不产生歧义；连接级动作仍是显式 `{tool, arguments}` 透传。
- [x] 3.8 端到端（受控 adapter，不调用第三方）：受信身份下，已分配且获准的智能体经读动作调通一个已发布工具；未分配、连接停用、授权撤销、`read_execute` 关闭时分别被拒。

## 4. 写动作契约保持（`mcp-connection-integration`）

- [x] 4.1 既有「以发现绑定作为 `tools.call` 载体」的用例已迁移到 `tools.read`，继续覆盖授权、配额、撤权、停用、跨租户与无身份等通用闸门。
- [x] 4.2 「MCP 工具调用必须审批」的契约由**连接级** `tools.call` 覆盖（管理路径、工具入口路径、智能体路径三处各一条），未删除该覆盖。
- [x] 4.3 迁移后 `write_actions == {tools.call}`、审批要求与 `tools.call` 的风险等级（high、write=True）断言仍存在且通过。

## 5. 控制台字段（`external-system-access-console`）

- [x] 5.1 `channel/web/static/js/external-connections.js`：移除只读声明 section、字段、客户端校验分支、`FIELD_TOKEN_KEY` 条目、`ecBuildConfig()` 写入与相关上界常量。
- [x] 5.2 i18n 三语删除 `ec_section_read_only`、`ec_field_read_only_tools`、`ec_validate_read_only_tools`、`ec_validate_read_only_tool`、`ec_validate_read_only_tool_duplicate`，并同步 `tests/fixtures/console_i18n_snapshot.json`（`test_console_i18n_parity.cjs` 10 passed）。
- [x] 5.3 前端测试：MCP 表单不含逐工具声明输入；保存请求的 `config` 只含连接级键（stdio 与远程两种形态），遗留草稿里的旧键既不校验也不重提交。
- [x] 5.4 卡片执行状态行（`ec_card_exec_read_only`）不受影响，按读切片与分配正确显示。

## 6. 验收与证据

- [x] 6.1 针对性回归：`tests/test_external_mcp_adapter.py`、`tests/test_external_connection_assignment_runtime.py`、`tests/test_external_connections_api.py`、`tests/test_external_connections_menu.py`、`tests/test_external_authorization.py`、`tests/test_external_risk.py`、`tests/test_execution_isolation.py`，以及 `tests/test_external_connections_frontend.cjs`、`tests/test_external_connections_browser.cjs`、`tests/test_console_i18n_parity.cjs`；结果与变更前基准的对比见 `evidence-5/regression.md`（仅两条变更前既有缺陷失败）。
- [x] 6.2 真实浏览器证据：MCP 表单不含逐工具声明、保存成功、卡片与表单状态一致；截图与 `results.json` 落在 `evidence-5/browser/`，已用 `mcp-connection-level-only.png` 替换失效的声明字段截图。
- [x] 6.3 严格校验 `openspec validate add-external-mcp-readonly-tool-execution --strict`，并确认两个被修改能力的 spec 与实际证据一致。
- [x] 6.4 记录未实施项：MCP `write_execute` 未开放；远端写工具经读动作执行不再有本地逐工具判定；在未开放 `read_execute` 的部署上，智能体仍看不到任何发现工具。
- [x] 6.5 更新 `evidence-6/live-deployment-diagnosis.md`：把「`read_only_tools` 声明」一项改为核对「配置中不存在该字段」，并说明剩余闸门与最小运维动作。

## 7. 分配即该连接的授权（`mcp-connection-integration`）

现场核对发现最后一处阻塞不在本 change 的字段上：连接 `conn_cN-lhbMpQlwpiL7k`（`weknora-rsmagent`）已分配给 `tax-health-check-test15`、12 个远端工具已发现、`read_execute` 已开放，但 `authorized_tools()` 仍返回空 —— 租户 `test15` 的 `tenant_resource_grants` 里没有 `external:mcp:mcp.tools.read`。按「有智能体的授权就可以了」，分配本身就是这份连接的授权，不再叠加逐资源授权。

- [x] 7.1 `integrations/external/authorization.py`：`may_execute()` 增加第四条入口 `_assignment_authorizes()`，与「个人连接凭所有权」同源；只在 `REASON_ASSIGNED`（`configured=1` 且当前 Agent 在关系内）时成立，并仍要求 `tool.execute`。
- [x] 7.2 边界（不因分配放宽）：`configured=0` 存量连接、空分配、分配状态缺失、分配查询失败、无受信 Agent 上下文、跨租户 Agent 一律保持逐资源授权要求。
- [x] 7.3 边界（不替代其他条件）：功能权限、执行切片、风险等级、审批与配额照旧逐次判定（分配在 `may_execute` 内只放行授权；runtime 的切片/风险/审批/配额闸门顺序不变）。
- [x] 7.4 两处调用点传入 `connection_id`：`integrations/external/tools.py#authorized_tools()`（投放/可见）与 `integrations/external/runtime.py#invoke()`（实际调用），使可见与可调对同一条规则作答。
- [x] 7.5 测试：`tests/test_external_authorization.py` 新增「分配即该连接的授权」10 条（含正向、列表一致、功能权限、关系外、空分配、存量连接、无 Agent、跨租户、切片未开、配额仍在）；`tests/test_external_connection_assignment_runtime.py` 的原「无读授权即不可见」用例改写为「分配即可见、无功能权限仍不可见」，并修正受影响的投放集合断言。

## 8. 模型面工具名符合线路契约（`mcp-connection-integration`）

现场报告（2026-09-27 19:03）：在智能体里问「你可以使用的mcp工具？」得到
`Invalid 'tools[18].function.name': string does not match pattern '^[a-zA-Z0-9_-]+$'`。名字里的 `.` 来自动作标识（`tools.read`）与旧分隔符 `.`。同一错误在 16:46 已经出现（当时只投放 4 个连接级工具），因此这是**变更前既有缺陷**：授权链打通后工具真正进入模型面，它才变得无法回避。拒绝的是整次请求，不是那一个工具。

- [x] 8.1 契约常量落在 `agent/tools/base_tool.py`：`TOOL_NAME_RE`（`^[a-zA-Z0-9_-]+$`）、`MAX_TOOL_NAME = 128`、`is_wire_safe_name()`。上限取现场提供方的 128 并写明它与 OpenAI 文档的 64 不同源，避免后来者把两者当成同一个数。
- [x] 8.2 `agent/tools/mcp/external.py#tool_name()` 改写为按契约组合：分隔符 `.` → `_`；每一段经 `_wire_part()` 规范化；远端段经 `_remote_segment()` 处理长度预算，预算按**模型可见的**名字（含 `external_` 前缀）计算，而不是按裸绑定名。
- [x] 8.3 改写即丢信息：远端名被改写或超长时，段尾附加原始远端名的 SHA-1 前 12 位（`REMOTE_DIGEST_LEN`）。名称因此只由输入决定、与发现顺序无关；`tools.read` 与 `tools-read` 不会共用一个名字。
- [x] 8.4 无法在契约内命名时返回 `""`（空连接标识/空动作也返回 `""`），适配器据此跳过并记录，而不是投放一个会让整次请求失败的名字。
- [x] 8.5 适配器 `_discovered_bindings()` 对同一连接内重名做兜底跳过并记录；`available_tools()` 在跨连接汇总处对同名做兜底跳过并记录（两个连接共用一个名字时模型无法分别寻址）。
- [x] 8.6 纵深防御：`agent/protocol/agent_stream.py#build_tools_schema()`（真正发出工具定义的那一层）在名字不合规时**不投放该工具并记录 error**。一个坏名字从此只损失一个工具，不再损失整轮对话。既有「动态 schema 优先」的行为不变。
- [x] 8.7 测试：新增 `tests/test_tool_name_wire_contract.py`（14 条：组合、预算、摘要、空段、边界闸门）；`tests/test_external_mcp_adapter.py` 新增 4 条投放面契约用例（非法字符、同名抹平、超长、上界组合）。
- [x] 8.8 既有用例不再手写点号名：5 个测试文件的 44 处改为经 `tool_name()` 构造（`_mcp_name` / `_model_name` 两个局部助手），名字拼写本身由 8.7 的新文件钉住。授权 id（`external:mcp:mcp.tools.read`）是 `(kind, action)` 派生，未改动。
- [x] 8.9 现场复验：`evidence-7/live_tool_name_wire_contract.txt`（投影 15 个名字全部合规；把该 schema 原样发给现场提供方由 400 变为 200）；`evidence-7/browser/chat-mcp-tools-19-18.png`（同一会话问同一句话，智能体列出 12 个知识库工具并说明另一个连接为何不可用）。
