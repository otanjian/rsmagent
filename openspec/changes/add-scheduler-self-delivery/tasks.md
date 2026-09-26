# 任务：站内自推送定时任务

## 1. 事实核对（开工前）

- [x] 1.1 核对投递层对 web 的既有支持：`integration.py` 的 `_is_channel_ready`（web 恒就绪）、四个 `_execute_*` 的 web 分支、`_remember_delivered_output`（web 总是落会话历史），确认本 change 不改投递（design D1）。
- [x] 1.2 核对创建侧两条链路：`SchedulerTool._create_task` 在不传目标时的默认上下文取值，以及控制台创建弹窗强制「IM 实例 + 接收人」的路径。
- [x] 1.3 核对 web 被排除的两个位置：`scheduler_targets.py::_NON_DELIVERY_TYPES` 与 `recipient_store.py::remember`；确认本 change 继续排除（design D3）。
- [x] 1.4 核对可信会话解析缝：`authorization.py::_require_session_scope(ctx, session_id, agent_id)` 的返回与校验范围，以及它读取的 `agent/memory/conversation_store.py`（`seam:conversation-store`，`_dimensions()` + `ALWAYS_SCOPED_DIMENSIONS`）如何按租户/owner 收窄 `sessions`；确认 self 目标直接复用这条解析结果而不另立校验、不新增键维度（design D2）。
- [x] 1.5 核对 `_PROTECTED_CREATE_FIELDS` 与 `TaskAccessService.create_task` 的盖章行为，确认站内自推送不新增旁路。
- [x] 1.6 核对既有任务的编辑面缝：`tests/test_scheduler_web_update.py`（`seam:scheduler`）覆盖的 `/api/scheduler/update` 合并语义必须让 self 目标的 `instance_id` / `notify_session_id` 在无关编辑后原样保留，且不能被改派为目录联系人（design D5）。

## 2. Agent 工具：默认投回当前会话

- [x] 2.1 `agent/tools/scheduler/scheduler_tool.py`：新增 `deliver_to` 参数（`current_session` 默认 / `recipient`），加入 `params.properties` 并保持默认值语义。
- [x] 2.2 重写工具 `description`：把「投回当前会话（默认）」放在首位，明确「用户说推给我时不要索要渠道/接收人」；`list_recipients` 的说明限定为「推送给别人时」。
- [x] 2.3 `_create_task`：`deliver_to=current_session` 走既有默认上下文路径并跳过跨渠道目标解析；`deliver_to=recipient` 保留既有目录校验分支。不传 `deliver_to` 的老调用按「有目标即 recipient、无目标即 current_session」兼容推断。
- [x] 2.4 创建成功回执：`current_session` 时显示「投递目标: 本站（当前会话）」，`recipient` 时保持既有接收者描述。
- [x] 2.5 目标缺失/非法错误改为两条可执行选项（本站默认 / 让别人先联系智能体），移除「请到控制台手动创建」式文案。
- [x] 2.6 `_list_recipients` 的错误与返回值文案与 2.2 的新语义对齐（仍只在 web 会话可用）。
- [x] 2.7 `agent/tools/scheduler/task_store.py::_ensure_store_dir`：修掉 Windows 下 `os.path.dirname(os.devnull) == ''` 触发的 `[WinError 3]`，否则工具路径在测试夹具下无法落盘。

## 3. 服务端：self 目标解析与创建放行

- [x] 3.1 `channel/web/fork/scheduler_targets.py`：新增站内自推送常量（`SELF_INSTANCE_ID="web"`、`SELF_TARGET_NAME`）与 `resolve_self_target(ctx, session_id, agent_id)`，内部经 `_require_session_scope` 同源的 `_owned_context_target` 校验并解析；`receiver` / `session_id` 取解析结果，不取请求值。
- [x] 3.2 `SchedulerTargetService.list_instances(ctx)`：在首位注入合成实例「站内消息（推送给当前会话）」（`instance_id="web"`、`is_self=true`），不读取目录。
- [x] 3.3 `SchedulerTargetService.resolve_target`：识别 `instance_id == "web"` 时走 self 分支，返回与 3.1 一致的可信元数据；其余实例保持既有目录校验。self 目标 Agent 解析在未给 `agent_id` 提示时回退遍历该成员的启用 Agent 绑定。
- [x] 3.4 `channel/web/fork/handlers/scheduler.py::_create_task_for`：放行 self 目标（`instance_id == "web"`），要求请求携带当前 `session_id`；`_create_whitelisted_action` 对 self 目标使用服务端解析的 `channel_type="web"` / `session_id` / `receiver`。
- [x] 3.5 受保护字段与「客户端 `channel_type` 必须与解析结果一致」的校验对 self 目标同样生效（`receiver` 不作为证据，不一致即拒，`invalid_target`）。
- [x] 3.6 校验失败按既有错误形状作答（`invalid_target` / `target_not_found`），不写任务、不夹带内部信息。

## 4. 前端：创建弹窗的「本站」目标

- [x] 4.1 合成项落地在 fork 补丁 `static/js/fork/tasks-console.js`（而非上游 `static/js/views/tasks-modal.js`）：包装 `initDropdown` 在创建模式给实例列表补标签并预选 `web`，包装 `filterTaskRecipients` / `refreshTaskRecipients` 在 self 目标下固定接收人为「我（本站）」、隐藏外部联系人刷新；保存仍走上游 `saveTaskEdit` 的创建分支，提交 `action.instance_id="web"` 与当前 `session_id`。包装上游文件会污染 `TasksPagePortDriftTests` 登记的上游摘要，fork 补丁也正是本仓库放 fork-only 行为的位置。
- [x] 4.2 `static/js/fork/tasks-console.js`：确认合成实例与既有 `initDropdown`/锁定接缝不冲突（创建模式仍可切换目标，编辑模式仍冻结；编辑既有任务不被 self 目标抢占）。
- [x] 4.3 `static/js/i18n/tasks-records.js`：新增 `task_self_*` 文案（zh / zh-Hant / en），空态与提示改为「可推送到本站 / 或选择已联系过的渠道联系人」，并同步 `tests/fixtures/console_i18n_snapshot.json`（i18n parity 快照）。
- [x] 4.4 无任何 IM 实例时，弹窗默认选中「本站」，保存不再因缺少实例/接收人而阻塞。

## 5. 测试

- [x] 5.1 `tests/test_scheduler_tool_dispatch.py`：新增「web 上下文 `deliver_to=current_session` 创建成功，`channel_type=web`、receiver=会话 id、scope=personal」；保留「跨渠道 `deliver_to=recipient`」回归（含受信目录命中、目录外拒绝、缺目标拒绝、非法 `deliver_to`）。
- [x] 5.2 `tests/test_scheduler_create_scope.py`：新增「控制台 self 目标创建成功」；「伪造他人/不存在/非 web 的 session_id 被拒且不写任务」；「客户端传受保护字段或伪造 receiver 被拒」；「无 agent_id 提示时按会话行解析 Agent」。
- [x] 5.3 新增/扩展用例断言 web 仍不进入 `/api/scheduler/recipients` 的输出（跨渠道目录不含任何 web 目标，`instance_id=web` 不是合法目录键）。
- [x] 5.4 配额回归：配额耗尽时 self 目标创建与跨渠道创建同样被拒（工具路径与 HTTP 路径各一）。
- [x] 5.5 `tests/test_scheduler_frontend.cjs`：新增「创建弹窗存在本站目标并预选、选中后固定接收人、刷新不夺回、保存提交 `instance_id=web` + 当前 `session_id`、编辑既有任务不被抢占」。
- [x] 5.6 用 `tests/_helpers.py` 的 `WebAppHarness` 在真实 HTTP 上断言，不伪造 handler 返回。
- [x] 5.7 `tests/test_scheduler_web_update.py`（`seam:scheduler`）：新增「self 目标任务仅改名后，`instance_id='web'` 与 `notify_session_id` 原样保留、receiver 不被改派」的编辑面回归。
- [x] 5.8 `tests/test_scheduler_cross_channel_recipients.py`：把两条拒绝文案的断言更新为新措辞（原断言的是被本 change 替换掉的英文提示）。

## 6. 校验与交付

- [x] 6.1 `python scripts/check_change_deltas.py add-scheduler-self-delivery`：`OK (proposed)`，delta 与基线一致，且 `seam:conversation-store` / `seam:scheduler` 两行均被本 change 指名。
- [x] 6.2 运行受影响的后端用例（scheduler 相关）与前端 `.cjs` 套件，与改动前基线逐条对比，不引入新增失败（见 6.5）。
- [ ] 6.3 手动验收：在 Web 会话里说「每天 9:00 把未完成的 bug/需求推给我」，确认无需提供渠道/接收人即创建成功，且回执/列表显示「本站」。—— 待有可用运行实例时执行（需要真实 LLM 会话）。
- [ ] 6.4 手动验收：控制台创建弹窗在无 IM 通道时可创建「本站」任务。—— 同上；自动化已在 5.5 覆盖保存载荷与预选行为。
- [x] 6.5 记录验收证据（命令、计数、SHA、真实操作与脱敏结果）：
  - 基线 SHA：`83e10d47`（工作区改动未提交）。
  - 后端：`python -m pytest tests/ -q -k "scheduler"` → **264 passed**（0:09:58）。
  - 前端：`node --test tests/test_scheduler_frontend.cjs tests/test_console_i18n_parity.cjs tests/test_console_i18n_coverage.cjs tests/test_desktop_scheduler_poll.cjs tests/test_feature_action_clients.cjs tests/test_i18n_tenant_channel_keys.cjs tests/test_i18n_tenant_editor_keys.cjs tests/test_i18n_external_identity_keys.cjs tests/test_recovered_pages_frontend.cjs tests/test_fork_fragments.cjs` → **99 passed**。
  - 全量 `-k "scheduler or task_page or upstream_drift or web_database_capability"` 初跑 4 failed；其中 3 条在 `git stash` 的干净树上同样失败，与本 change 无关（`TasksPagePortDriftTests::test_the_registered_upstream_sources_are_unchanged`；`test_web_database_capability_acceptance` 两条因 `/api/memory` 在 Windows 临时目录写 `.tmp-*` 报 `[Errno 2]` 返回 503）；第 4 条 `test_scheduler_cross_channel_recipients` 由本 change 的文案变更引起，已在 5.8 修正。
  - delta 门：`python scripts/check_change_deltas.py add-scheduler-self-delivery` → `OK (proposed): … deltas consistent with the baseline, every conflicted file covered`。
  - 关键断言（脱敏）：self 创建任务 `action.channel_type="web"`、`action.instance_id="web"`、`action.receiver=<会话 id>`、`action.notify_session_id=<会话 id>`、`scope="personal"`、`owner.user_id=<调用者>`；伪造他人 / 不存在 / 非 web 会话 → `target_not_found`（404）且任务数为 0；客户端 `channel_type` 与解析结果不一致 → `invalid_target`（400）；受保护字段（`owner`/`scope`/`tenant_id`）→ `invalid_request`（400）；配额 =1 时第二个 self 任务 → `quota_exceeded`（409）。
