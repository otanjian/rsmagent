# 设计：站内自推送定时任务

## D1 缺口在「创建目标」，不在「投递」

现场的第一反应容易是去查投递为什么失败。实际投递层对 web 的支持是完整的：

- `agent/tools/scheduler/integration.py::_is_channel_ready` 对 `channel_type == "web"` 直接返回 `True`（web 会话的消息总是被持久化进会话历史，空闲客户端下次轮询 `/poll` 时取回），并明确注释了「不要用内存 `session_queues` 判定 web 就绪」这条既有修复。
- 同文件 `_execute_agent_task` / `_execute_send_message` / `_execute_tool_call` / `_execute_skill_call` 都有 `channel_type == "web"` 分支（生成 `request_id` 并登记 `request_to_session`），且 `_remember_delivered_output` 对 web **总是**落会话历史。
- 前端 `static/js/chat/scheduler-notify.js` 会轮询全局运行台账并对 web 投递强制通知，`runChannelDisplay` 专门把 web 运行显示成「本站消息」。

所以真正缺失的是「创建一个投递给自己的任务」这条路径：

- `SchedulerTool.description` 只写了 `For Web cross-channel delivery, call action='list_recipients' first`，没有一句说明「省略目标即投回当前会话」。
- `channel/web/fork/scheduler_targets.py` 的 `_NON_DELIVERY_TYPES = frozenset({"", "web", "unknown"})` 在 `_deliverable` 与 `resolve_target` 两处把 web 过滤掉，控制台因此永远拿不到「本站」这个实例。
- `agent/tools/scheduler/recipient_store.py::remember` 对 `channel_type in {"unknown", "web"}` 直接返回 `None`，`integration.attach_scheduler_to_tool` 也以 `channel_type != "web"` 为前提写目录。

本 change 只补创建侧，不碰投递与执行（见 D4 的边界）。

## D2 站内自推送目标由服务端从认证会话解析

目标必须由服务端解析，客户端只提供「是哪一个会话」，不提供「投给谁」。

服务端已有的可信解析缝是 `channel/web/fork/authorization.py::_require_session_scope(ctx, session_id, agent_id) -> str`（对外入口是同一模块的 `_owned_context_target(ctx, session_id, agent_id)`，它把「Agent 可见性」与「会话持久归属」两级校验合成一次调用）：它一次完成三件事——租户绑定、会话对调用者的可见性、以及 `sessions` 表里「owner = 调用者且为 web 会话」的持久归属校验——并返回解析出的 agent id。站内自推送正好需要这三件事，所以直接复用它，不再另立一套校验：

| 事实 | 来源 |
| --- | --- |
| tenant_id / owner | `ctx`（`_db_scope()` 内已验证的认证上下文） |
| `session_id` 归属与 `channel_type='web'` | `_require_session_scope` |
| `agent_id`（任务执行/归属 Agent） | `_require_session_scope` 的返回值 |
| `receiver` | 等于 `session_id`（web 会话的 receiver 就是会话 id，见 `fork/runtime.py` 对 `context["receiver"] = session_id` 的赋值） |

其中会话归属读取的是 `agent/memory/conversation_store.py` 落库的 `sessions` 行：该文件正是 `seam:conversation-store` 挂载的缝——上游的单一全局 `agent_id` 存储与 fork 的租户复合键（`_dimensions()` + `ALWAYS_SCOPED_DIMENSIONS`）在这里并存。本 change 只**读**这条缝已有的解析结果，不改其维度模型、不绕过其租户/owner 收窄，也不为 self 目标另建键空间。

因此 self 目标的 **receiver 不接受客户端传值**：请求里带的 `action.receiver` 只用于存在性校验，最终写入的值取自服务端解析。`owner` / `tenant_id` / `scope` / `revision` 继续由 `_PROTECTED_CREATE_FIELDS` 拒绝，`TaskAccessService.create_task` 按 `actor` 盖章。

常量选 `instance_id = "web"`：它与投递层的既有语义一致——`_resolve_delivery_channel(channel_type="web", instance_id="web", receiver=session_id)` 会经 `manager.get_channel("web")` 命中正在运行的 web 实例，与跨渠道任务走的是同一条解析路径，无需为 self 目标新增投递分支。

## D3 不把 web 写进接收者目录

一个自然的「省事」做法是把 web 会话也 `remember` 进 `RecipientStore`，让控制台的两步选择器直接复用。这个做法被否掉：

- 目录是**跨 Agent 共享**的联系人集合，语义是「这个通道实例见过这个人」。把每个成员的每个 web 会话塞进去，会让「其他人的会话」出现在选择器里——即使服务端在 `list_recipients` 按实例收窄，也把「会话身份」和「通道联系人」两种东西混进同一个 `(instance_id, receiver)` 键空间。
- web 会话是**每用户私有**的：它的 receiver 就是某个人的会话 id，暴露它等价于暴露会话标识；而 `list_recipients` 的既有契约已明确「MUST NOT 拆解由冒号拼接的 key、MUST NOT 把全局目录发到客户端过滤」。
- self 目标根本不需要目录：它只对发起创建的本人可见可用，由 D2 的认证解析直接合成。

所以 web 继续留在 `_NON_DELIVERY_TYPES` 与 `remember` 的排除项里；站内自推送在 `SchedulerTargetService` 里是一个**合成的、仅本人的**目标，不写入、不读取目录。

## D4 Agent 工具的默认语义与描述

`SchedulerTool._create_task` 在**不传** `channel_type` / `receiver` 时，本就用当前上下文构造目标（`notify_session_id = context["session_id"]`、`receiver = context["receiver"]`、`channel_type = self.config["channel_type"]`，后者由 `attach_scheduler_to_tool` 从上下文/配置填入）。也就是说「投回当前会话」这条代码路径已经存在，缺的是：(a) 显式语义参数以免模型猜测，(b) 描述引导，(c) 一个把「目标就是本站」讲清楚的回执。

- 新增 `deliver_to` 枚举：`current_session`（默认）与 `recipient`。
  - `current_session`：不解析跨渠道目标，走既有默认路径；回执写明「投递目标: 本站（当前会话）」。
  - `recipient`：保留既有跨渠道分支（`channel_type` + `receiver`，经可信目录校验），供「推送给别人」使用。
- `description` 重写，把两条路径的适用条件讲在前面；`list_recipients` 的描述与工具说明限定为「推送给别人时」。
- 目标缺失/非法时的错误文案给出「本站（默认）」与「给别人（需先让对方联系智能体）」两条可执行选项，删除「请到控制台手动创建」这类把责任推回用户的措辞。

工具在所有渠道可用：IM 会话里 `current_session` 就是投回该 IM 会话（既有行为），Web 会话里就是投回本站。

## D5 控制台创建弹窗的「本站」目标

创建弹窗（`static/js/views/tasks-modal.js` + fork 补丁 `static/js/fork/tasks-console.js`）保持既有两步选择器结构，只在实例列表首位注入一个合成项：

- 实例值为 `instance_id = "web"`，标签「站内消息（推送给当前会话）」；
- 选中后接收人步骤固定为「我（本站）」，不再渲染外部联系人下拉；
- 保存时提交 `action.instance_id = "web"` 与当前会话 `session_id`（前端从当前会话状态取，服务端仍重新校验）。

编辑面不动：`fork/tasks-console.js::lockDeliveryTarget` 对既有任务继续只读展示投递目标，`TaskAccessService.update_task` 仍拒绝改派（`forged_field`）。这与「站内自推送目标只对本人」是一致的——目标在创建时定死。

## D6 前置、边界与未决参数

- **复用的门槛**：创建仍以 `TaskAccessService.create_task` 为唯一写入口（成员、`agent.use`、配额、owner 快照、`scope`、revision、审计）。站内自推送不新增授权判定点，只是多了一个合法目标来源。
- **执行时不变**：触发与手动运行仍在 `integration.py` 的 trusted execution identity 下重验 owner 与 readiness；web 恒就绪是既有语义，本 change 不修改。
- **未决实施参数**：多会话用户的默认取值定为「当前正在对话的会话」（用户已确认）；本 change 不引入「站内信箱会话」这类兜底目标。若会话在触发前被删除，web 投递不会失败重试，消息无落点——这是「当前会话」语义的已知取舍，留待后续 change 评估。
- **兼容**：不传 `deliver_to` 的老调用按 `current_session` 处理，与「不传目标即投回当前会话」的既有代码行为一致，不产生语义漂移。
