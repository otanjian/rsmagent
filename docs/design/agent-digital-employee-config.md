# 智能体数字员工能力（agent-digital-employee-capabilities）实施与配置说明

日期：2026-09-09。承接 openspec change `agent-digital-employee-capabilities` 的 Phase 1 运行时背台实施。本文记录：新字段含义与继承规则（4.4）、启用门槛证据与缺省兼容策略（4.2/4.3）。本文不复制 OneAgent 凭据或客户数据。

## 1. 变更范围（本次已落地）

- `AgentProfile` 新增数字员工字段：`position`、`category`、`tags`、`greeting`、`persona_summary`、`scene_id`、`knowledge_ids`、`sops`、`tools_allowlist`、`tools_denylist`。旧配置缺省兼容（未配置即保持改造前行为）。
- 新增 `agent/effective_capabilities.py`：基于 `AgentProfile` + 可选场景计算**有效能力**（继承/覆盖/并集法则），并提供 `filter_tools` / `is_tool_allowed` 供装配与分发共用。
- 运行时真裁剪：
  - 工具装配路径（`AgentInitializer._apply_agent_tool_policy`）按有效 allow/deny 过滤枚举可见工具集。
  - 工具调用入口（`AgentStreamExecutor`）二次校验，拒绝白名单外/黑名单内调用并返回明确错误。
  - MCP 延迟注入（`ToolManager.sync_mcp_into_agent`）按智能体策略过滤，防绕过。
  - 会话构建注入员工上下文（`AgentBridge._apply_employee_context`）：职位/人设摘要/问候语/SOP 追加到 `extra_system_suffix`，与场景 `extra_system_suffix` 共存，不自动代发用户消息。
- 管理面：`AgentAdminService.create_agent` / `update_agent` 纳入新字段，`/api/agents` create/update 载荷透传，拒绝无效 `scene_id`，`revision` 冲突规则沿用。

## 2. 字段与继承规则

| 字段 | 类型 | 缺省 | 运行时影响 |
| --- | --- | --- | --- |
| `position` | string | 无 | 注入员工上下文（职位段） |
| `category` | string | 无 | 管理分类（可复用场景 categories，允许空） |
| `tags` | string[] | `[]` | 管理标签 |
| `greeting` | string | 无 | 注入问候语段；同时作为 Web 空会话的简短自我介绍 |
| `usage_hint` | string | 无 | 仅 Web 空会话展示「使用说明」，最多 200 字；不注入系统提示 |
| `suggested_questions` | string[] | `[]` | 仅 Web 空会话展示 1–4 条建议问题（每条最多 200 字）；不注入系统提示 |
| `persona_summary` | string | 无 | 注入人设摘要段 |
| `scene_id` | string | 无 | 解析场景做继承；无效拒绝 |
| `knowledge_ids` | string[] | `None` | （延后）知识条目限定，Phase 1 仅持久化 |
| `sops` | string[] | `[]` | 注入 SOP 清单段 |
| `tools_allowlist` | string[] | `None` | 非空则仅暴露白名单内工具 |
| `tools_denylist` | string[] | `[]` | 并集合并后排除 |

**有效能力解析法则**（`resolve_effective_capabilities`）：

- 人设摘要：非空员工字段优先，否则继承场景 `system_prompt`。
- 工具白名单：非空员工允许清单优先，否则继承场景 `tools`；均缺省为 `None`（不额外收窄）。
- 工具黑名单：员工黑名单 ∪ 场景黑名单。
- SOP：非空员工清单优先，否则继承场景 `sops`。

**举例**：某智能体 `scene_id=procurement`、`tools_allowlist=["read"]`、`tools_denylist=["bash"]`，场景 `tools_denylist=["rm"]`。则有效白名单=`read`，有效黑名单=`{bash, rm}`，装配后模型仅见白名单且黑名单在分发入口兜底拒绝。

## 3. 缺省兼容策略（4.3）

未配置任何新字段的智能体：

- `tools_allowlist=None`、`tools_denylist=()`、`persona_summary=None`、`sops=()`。
- 工具可用性与技能保持在改造前行为：无白名单即暴露默认工具集，黑名单为空即不排除，技能选择 `None` 即「全部已安装」，知识 `knowledge_ids` 不额外收窄。
- `_apply_agent_tool_policy` 在无策略时走快速路径（不做过滤），避免对存量智能体引入额外开销与行为变化。

## 4. 启用门槛证据（4.1 / 4.2）

- 工具硬过滤：`tests/test_agent_capability_enforcement.py`——白名单裁剪、黑名单排除、伪造调用被拒、旧配置兼容、场景继承/合并；通过 `initialize_agent` / `get_agent` 真实路径验证装配不泄漏被禁工具、人设/SOP 注入到 `extra_system_suffix`。
- MCP 防绕过：`tests/test_mcp_tool_prefix.py::test_mcp_sync_respects_agent_allow_deny`。
- 模型/解析/管理持久化：`tests/test_effective_capabilities.py`——字段解析、场景 merge 矩阵、admin create/update 往返持久化、无效 `scene_id` 拒绝、`team.compact` 保留新字段。
- 上述全量在真实 agent 构建路径下通过（`test_agent_initializer_routing` 同款集成测试）。

## 4.1 手工验收清单（4.1 任务）

> 面向实施者的人工冒烟清单；自动化可覆盖的部分已在第 4 节证据中标注。验收通过前不得宣称 Phase 1 生产可用。

1. **新建岗位化智能体**：在 Web 智能体管理「概况」填写职位/分类/标签/问候/人设摘要/关联场景，保存成功；刷新后字段回填。
2. **绑定场景继承**：选择某个含 `extra_system_suffix` / 场景工具的场景后，在「能力」页签可见场景继承；空字段按「场景覆盖」规则取值（见第 2 节继承规则）。
3. **工具子集**：在「能力」页签为某智能体勾选工具 `允许`/`拒绝`，保存后 `/api/agents` 载荷与 `AgentProfile` 持久化一致。
4. **开聊仅见白名单工具**：以该智能体开聊，可见/可调用工具仅剩白名单集合；黑名单工具在枚举与调用入口均被拒绝并返回明确错误。
5. **旧智能体行为不变**：对未填写数字员工字段的存量智能体，开聊行为、可用工具集、技能行为与改造前一致（白名单为空即全量、黑名单为空即不排除、技能 `null` 即全部）。
6. **MCP 同步防绕过**：若某智能体绑定 MCP 工具，且该工具在其黑名单内，确认 MCP 加载完成后仍不可见/不可调用。

## 5. 已知边界（Phase 1）

- **知识检索限定（2.5）** 延后：`knowledge_ids` 语义在现状代码库未明确定义（知识库为 `knowledge/` + agent 自维护的 `index.md`），无法可靠映射 ID→检索范围；待 Phase 2 UI 落地知识条目选择时再定义并接检索过滤。
- 工具过滤为**运行时硬裁剪**（枚举 + 入口双重生效），不引入 feature flag；缺省兼容即默认关闭（存量无字段）。
- Web 概况/能力页签与工作台投影、i18n 已在 Phase 1 落地（任务 3.1–3.5）。Desktop `AgentsPage` 仅对齐共享类型与字段语义，概况/能力页签详细 UI 待后续对齐（不阻塞 Web 交付）。

## 6. Phase 2 员工任务与运营视图（5.1–5.6）

**已落地：**
- **任务归属（5.1）**：`TaskStore` 按智能体工作区一份（`state_dir/scheduler/tasks.json`）；`/api/scheduler` GET 支持 `agent_id` 作用域/聚合，任务条目回填 `agent_id`。既有 `test_multi_agent_state_isolation` 覆盖多智能体任务存储隔离。
- **任务页签（5.2）**：Web 智能体详情新增「任务」页签（`agent-detail-tasks`，`renderAgentTasksPane`），按归属智能体拉取任务列表，复用 run/toggle 路由并回传归属 `agent_id`。越权校验沿用各 handler 的 `_require_agent_action`。完整「创建表单」沿用全局任务编辑弹窗。
- **触发裁剪（5.3）**：`_execute_agent_task` 以 `context[agent_id]` 交给 `agent_reply`，经 `get_agent→_apply_agent_tool_policy` 完成装配期裁剪；新增 `test_scheduled_agent_task_fires_under_owning_agent` 验证归属路由。智能体归档/删除由 roster 再同步 `stop_scheduler`（`web_channel._reconcile`）停用。
- **Desktop（5.4）**：任务页签本阶段仅 Web 提供；Desktop 仅对齐共享类型与字段语义，任务页签留后续对齐。
- **测试（5.6）**：`test_scheduled_agent_task_fires_under_owning_agent`（归属路由）；既有存储隔离/越权测试沿用。

**延后：**
- **记忆/对话归属只读视图（5.5）**：依赖 `conversation_store` 的 agent/session 归属模型，与任务页签解耦，另立后续项。

## 6.1 已知边界（Phase 2）

- **SOP 目录选择（6.1）**：当前 SOP 为自由文本 ID 增删；仓库存在 `/api/skills` 与 `scenes/skill_mapping.json` 可枚举源，但无统一「SOP 目录」端点，故 6.1 延后至枚举源统一后实现。
- **岗位种子智能体（6.2）**：依赖场景目录（`/api/scenes`）与场景工作台；本期未内置种子，避免在无凭据约束下引入不可运行实例。
- **能力页选择器打磨（6.3）**：属体验优化，不与功能正确性耦合，留待后续。
- **Phase 3（7.x）**：`visibility`/角色资源授权与 per-agent 覆盖全局参数，依赖 `role-resource-authorization`（或等价）智能体资源授权与执行隔离门槛；在外部变更落地前不宣称可用。

## 7. 对话引导字段（`usage_hint` / `suggested_questions`）

承接 openspec change `improve-agent-chat-onboarding`（capability：`agent-chat-onboarding`）。

- **归属**：仅 Web 普通单智能体（`agent_type=normal`）。Desktop 客户端、OpenCode 类智能体、场景应用与多智能体入口不适用。
- **编辑位置**：Web「智能体管理 → 概况」表单，与 `greeting` 同页保存；沿用原保存、取消、revision 冲突与权限校验，未新增预览或独立配置页。
- **展示位置**：空会话欢迎页依次为「头像与名称 → 自我介绍（`greeting`）→ 使用说明（`usage_hint`）→ 建议问题（最多 4 条，宽屏双列、窄屏单列）→ 原输入框」；首问后进入原消息页，历史会话不重复展示。
- **点击语义**：输入框为空、无附件、当前会话空闲时，点击问题即填入原输入框并调用既有 `sendMessage()`；有草稿或附件时入口禁用并提示先发送或清空，不覆盖草稿、不夹带附件。问题只接受普通文本，不解析 `/` 命令或 `@` 路由。
- **缺省与回退**：两字段均可选，未配置时使用中性默认文案；省略字段保留当前值，显式空串/空列表显式清空。无数据库迁移、无新增 feature flag。
- **校验**：`usage_hint` 最多 200 字；`suggested_questions` 最多 4 条、每条最多 200 字、纯文本。`agent/registry.py` 负责清洗与序列化，`agent/admin.py` 负责创建/更新/复制，`channel/web/fork/handlers/agents.py` 负责 API 与使用投影。
