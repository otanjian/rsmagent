## Why

现场现象：成员在 Web 会话里对智能体说「生成一个定时任务，每天上午 9:00，给我推送未完成的 bug 或者需求」，智能体回答「定时任务创建需要指定投递渠道和接收者（系统限制只能从 Web 控制台获取可接收人列表），我无法自行查询」，把创建甩回「请到控制台手动创建」或反过来向用户索要渠道类型与接收者 ID。而实际要投递的对象就是**当前这个会话里的这个人**。

根因不在投递层。投递与执行早已完整支持 web：`_is_channel_ready` 对 web 恒为就绪、`_execute_agent_task` / `_execute_send_message` 有 web 分支、控制台 `scheduler-notify.js` 轮询 web 运行并通知。缺口在**创建时可选择/可解析的投递目标**，两条创建链路都只把「跨渠道推送给已联系过智能体的 IM 联系人」做成一等路径：

- **Agent 工具**（`agent/tools/scheduler/scheduler_tool.py`）：工具描述只教了 `list_recipients` 跨渠道路径，没有任何「默认投回当前会话」的说明；`list_recipients` 也只在 web 会话可用且只返回 IM 联系人。目录为空时（没人从 IM 联系过智能体）模型只能判定「无法查询发送对象」。
- **控制台创建弹窗**（`static/js/views/tasks-modal.js`）：创建模式强制「选一个 IM 通道实例 + 该实例下的可信接收人」。而通道实例与接收人目录都在服务端**显式排除 web**（`channel/web/fork/scheduler_targets.py` 的 `_NON_DELIVERY_TYPES`、`agent/tools/scheduler/recipient_store.py` 的 `remember` 跳过 web）。

结果：最常见的「把结果推给我」既不是工具侧默认行为，在控制台里也选不到（Web-only 用户完全没有可用目标），用户被迫手动操作而多数情况下根本走不通。

## What Changes

- 新增一等创建路径**「站内自推送」**：投递对象就是发起创建的**当前 Web 会话**。
  - **Agent 工具**：新增 `deliver_to`（`current_session` 默认 / `recipient`）。默认在任意渠道下直接创建并投回当前会话，**不再索要** `channel_type` / `receiver`；`list_recipients` 的语义收窄为「只有当用户要推送给**别人**时才用」。工具描述与创建回执同步更新，目标缺失时给出可执行的两条选择，MUST NOT 再把用户推去控制台手动创建。
  - **控制台创建弹窗**：实例选择器首位新增「站内消息（推送给当前会话）」；选中后接收人固定为「我（本站）」，不再出现外部联系人两步选择。
  - **服务端解析**：self 目标由 `_require_session_scope(ctx, session_id, agent_id)` 校验并解析（tenant 绑定 + 会话可见性 + `sessions` 表 owner 为调用者 + `channel_type='web'`），客户端只可提交当前 `session_id`，`receiver` / `owner` / `tenant_id` / `scope` 一律不作为目标或归属证据。
- **不把 web 纳入跨渠道接收人目录**：`RecipientStore` 与 `_NON_DELIVERY_TYPES` 继续排除 web；站内自推送是服务端合成的目标，不是目录项，不会出现在任何人的可选接收人列表里。
- 写入仍唯一经 `TaskAccessService.create_task`，配额 / owner 快照 / `scope` / `revision` / 审计链路全部复用，不新增旁路。
- 控制台编辑面继续冻结投递目标（既有行为，web 任务尤其如此），不改动。

## Capabilities

### Modified Capabilities

- `database-scheduler-console`:
  - ADDED「站内自推送目标由认证会话解析创建」——把「投回当前会话」确立为与跨渠道并列的一等创建路径，规定其目标解析来源（服务端可信会话，客户端不可伪造）、web 继续排除在接收者目录之外、以及创建仍经同一授权/配额/审计链路。

## Impact

- **行为受影响**：新增站内自推送创建路径；Agent 工具的描述、参数与创建回执文案变化；控制台创建弹窗新增「本站」目标选项与相应空态文案。既有跨渠道创建的字段与语义保持不变。
- **不受影响**：投递与执行准备（`_is_channel_ready` 对 web 的就绪语义）、执行时 owner/资源重验、编辑面投递目标冻结（`tasks-console.js` 的 `lockDeliveryTarget`）、跨渠道接收者目录的范围与过滤、配额与审计。`RecipientStore` 的写侧（排除 web）不变。
- **代码面**：
  - 后端：`agent/tools/scheduler/scheduler_tool.py`（`deliver_to`、描述、默认投回当前会话、回执）；`channel/web/fork/scheduler_targets.py`（self 目标常量与解析、`list_instances` 注入合成项）；`channel/web/fork/handlers/scheduler.py`（`_create_task_for` / `_create_whitelisted_action` 放行 self 目标）。
  - 前端：`channel/web/static/js/fork/tasks-console.js`（创建弹窗的「本站」选项、接收人步骤与保存路径的 fork 补丁；上游 `static/js/views/tasks-modal.js` 不动，`TasksPagePortDriftTests` 登记的上游摘要因此保持不变）；`channel/web/static/js/i18n/tasks-records.js`（三语文案，并同步 `tests/fixtures/console_i18n_snapshot.json` 的 i18n parity 快照）。
  - 无新增配置项、无新增依赖、无数据库迁移（任务仍存于既有全局 `tasks.json`）。
- **测试面**：扩展 `tests/test_scheduler_create_scope.py`、`tests/test_scheduler_tool_dispatch.py`、`tests/test_scheduler_frontend.cjs`、`tests/test_scheduler_web_update.py`（编辑面保留 self 目标）与 `tests/test_scheduler_cross_channel_recipients.py`（拒绝文案对齐）；新增站内自推送的正向创建、伪造会话/归属被拒、web 不进入接收人目录、配额与跨租户回归用例。沿用 `tests/_helpers.py` 的 `WebAppHarness` 构造真实 HTTP 断言，不伪造 handler 授权结果。
