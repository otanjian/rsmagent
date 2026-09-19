# 验收证据：integrate-upstream-core-capabilities

本文件按 tasks.md §10 要求记录每批的代码 SHA、测试命令与计数、两端版本、真实步骤与回退结果。
**未执行的真实门槛必须留空并标注，不得以单测或模拟结果代替。**

## 0. 实施基线与依赖切片证据（task 1.1 / 1.2）

| 项 | 值 |
| --- | --- |
| 实施分支 | `integrate-upstream-core-capabilities` |
| 基线 SHA | `f5d7d764dcf15f328372ca2cd85fc6b30cfef8d1`（rdai 分支 HEAD） |
| 上游参考 | `8f1b19f1`（已合入；再合并 master 不会补齐本 change 的功能） |
| 隔离方式 | 从记录的 rdai 提交新建实施分支。工作区已有的在途变更（openspec 归档移动、todos UI、若干 spec 文本）保持未提交且不属于本 change，不得混入本 change 的提交 |
| Python | `.venv/bin/python`（默认 `python3` 在本机启动即被终止，不作为业务结论） |

### 依赖切片（前置门槛）证据

依赖切片按 capability 引用，不引用 PRD 编号。以下为“真实验收证据是否已存在”的核对结论；
**证据存在不等于本 change 的功能已验收**，只是允许本 change 在对应能力之上施工。

| 前置 capability | 现有证据来源 | 结论 |
| --- | --- | --- |
| `identity-session` / `desktop-tenant-context` | `tests/test_desktop_auth_flow.py`、`tests/test_web_database_capability_acceptance.py` | 存在；本轮以真实验收矩阵复核（§8.3），未完成前不开放 |
| `rbac-authorization` / `resource-execution-authorization` | `tests/test_scheduler_task_authorization.py` | 存在；P4 复用 `TaskAccessService.create_task` |
| `resource-quota` | `TaskAccessService` 配额闸口 + `check_scheduled_task_quota` | 存在；P4 创建路径覆盖 |
| `audit-log` | `TaskAccessService` 审计写入 + `tests/test_identity_audit*` | 存在；P4 创建、P6 删除各写一次 |
| `execution-isolation` | `agent/tools/scheduler/integration.py` 可信执行身份 | 存在；P5 归属快照依赖它 |

## 1. P1 动作投影与请求门禁

- 代码：`auth/capability_matrix.py`（八个独立 slice、`FEATURE_ACTIONS`、`feature_action_availability`、
  `RDAI_DISABLED_ACTIONS` 只关闭语义、未知键启动失败）、`auth/service.py::context_for_tenant`
  （追加 `feature_actions`）、`channel/web/static/js/functional-capabilities.js`。
- 命令与结果（本机，仓库根）：
  - `.venv/bin/python -m pytest -q tests/test_feature_action_projection.py tests/test_capability_matrix.py tests/test_route_registry.py` → 60 passed
  - `node --test tests/test_feature_action_clients.cjs` → 11 passed
  - 新进程验证：`RDAI_DISABLED_ACTIONS="scheduler.runs.list,scheduler.create"` 正确解析；
    `RDAI_DISABLED_ACTIONS="bogus.key"` 抛 `CapabilityConfigurationError`（启动配置错误）
- 真实门槛（未完成，未开放）：Web/Desktop 真机切换租户/登出/重连的陈旧响应丢弃演练。

## 2. P2 上下文控制

- 代码：`channel/web/fork/handlers/context.py`（用量 GET / 压缩 POST，真实 HTTP 状态与 `{status,code,message}`）、
  `channel/web/fork/authorization.py`（`_storage_agent_key`、`_owned_context_target` 精确 owner/tenant/agent 校验、
  `_require_owned_session` 收紧为同 Agent 匹配）、`channel/web/route_registry.py`（两条 session 路由先于
  `/api/sessions/(.*)` 声明）、`agent/protocol/agent.py`（`compact_context` 深拷贝 + 提交比较 + 提交后才写长期记忆）、
  Desktop `components/ContextUsagePopover.tsx` + `api/context.ts`（动作门禁与 `featureRevision` 陈旧丢弃）。
- 命令与结果（本机，仓库根）：
  - `.venv/bin/python -m pytest -q tests/test_session_context_scope.py` → 14 passed
  - `.venv/bin/python -m pytest -q tests/test_context_compaction_concurrency.py` → 4 passed
  - 上述两文件随本 change 全量相关集复核 → 198 passed（见 §7）
- 覆盖：非 owner、跨租户、同名会话、伪造 Agent、非法 Origin、无实时实例（`available=false` 不建实例）、
  生成中冲突、压缩期间新消息、双并发压缩、无内容可压缩。
- 客户端（task 3.5，两端）：
  - Web：`channel/web/static/js/functional-context.js` 挂载进 `#context-usage-host`，随现有会话生命周期
    装载/卸载；`chat.html`、`console.js`、`i18n/core.js` 同步接入。
  - Desktop：`components/ContextUsagePopover.tsx` 按 `session_context.usage` / `session_context.compact`
    逐项门禁，请求捕获 broker epoch、Agent、session 与 `featureRevision`，任一变化即丢弃陈旧响应。
  - 命令与结果（本机，仓库根）：
    - `node --test tests/test_functional_context.cjs` → passed
    - `node --test tests/test_desktop_core_integration.cjs` → 12 passed
- 真实门槛（未完成，未开放）：Web/Desktop 真机上的真实会话压缩演练（`session_context.usage` /
  `session_context.compact` 保持 `open={}`，投影为 `not_accepted`）。

## 3. P3 模型目录与回退链

- 代码：`channel/web/static/js/functional-models.js`（回退链增删排序启停、目录 seed/overrides/hidden 草稿、
  隐藏/恢复/恢复默认，保存不冻结未编辑预设）、`channel/web/static/js/console.js`（接入 `openChatFallbackModal`
  与 provider 卡片）、`channel/web/static/js/i18n/models-config.js`（三语言）、
  `channel/web/fork/handlers/pages.py`（`functional-*.js` 静态资产 + 缓存串发现式登记）。
- 命令与结果（本机，仓库根）：
  - `node --test tests/test_functional_models.cjs` → 15 passed
  - `node --test tests/test_member_model_catalog_frontend.cjs` → passed（既有目录用例复跑）
  - `node --test tests/test_console_i18n_parity.cjs` → 5 passed（新增键按“只增不改”写入快照，未改动既有译文）
- 真实门槛（未完成）：平台权限入口与两端真机回退演练（§8.3）。

## 4. P4 调度目标与创建

- 代码：`channel/web/fork/scheduler_targets.py`（`SchedulerTargetService`：实例、可信接收者、创建白名单投影）、
  `agent/tools/scheduler/recipient_store.py`（`list(instance_ids=...)` 过滤，保持无参兼容）、
  `channel/web/fork/handlers/scheduler.py`、`channel/web/route_registry.py`（三条路由，注册未开放）。
- 命令与结果（本机，仓库根）：`.venv/bin/python -m pytest -q tests/test_scheduler_create_scope.py` → 17 passed
- 覆盖：跨租户/他人实例、伪造目标与接收者、无 Agent `use`、硬配额、非法 Origin、绑定重验、无写重试。
- 客户端：Web 创建表单在 `functional-scheduler.js`（一次点击只提交一次，网络结果不明不重试）；
  Desktop 目标选择在 `TasksPage.tsx`（提交中禁用、关闭时不发请求）。
- 真实门槛（未完成，未开放）：真实渠道实例与真实接收者的创建验收（§8.3）。

## 5. P5 运行归属与数据访问

- 代码：`agent/tools/scheduler/run_repository.py`（`RunScope`/`RunGrant`/`RunQuery`、幂等扩展表与索引）、
  `agent/tools/scheduler/run_access.py`（`RunAccessService`，授权 SQL、范围先于分页）、
  `agent/tools/scheduler/integration.py`（`_record_scheduler_run` 写入执行时归属快照，写入失败不回退无范围查询）。
- 命令与结果（本机，仓库根）：`.venv/bin/python -m pytest -q tests/test_scheduler_run_scope.py` → 37 passed
- 覆盖：真实 SQLite 上的分页/排序/计数、空 Agent 参数不表示全局、默认 Agent、改绑后历史不重赋值、
  任务删除、历史隔离、无归属记录不可见、扩展写入失败、事务删除与 running 冲突、重复建表幂等。
- 真实门槛（未完成）：在历史功能仍关闭时执行真实测试任务并核对新增 run/scope（§8.3 / task 6.7）。

## 6. P6 运行历史与客户端

- 代码：`channel/web/fork/handlers/scheduler.py`（列表/详情/删除，`history_scope=attributed_only`，
  正文仅在单独会话权限下返回，否则预览且 `full_output=null`）、`channel/web/route_registry.py`、
  `channel/web/static/js/functional-scheduler.js`（历史/详情/删除 + 创建）、
  `channel/web/static/js/i18n/tasks-records.js`（三语言）、Desktop `api/client.ts`（`getSchedulerRunPage`
  与保留的 `getSchedulerRuns` 旧数组形状）、`TasksPage.tsx`、`hooks/useSchedulerNotifyPoll.ts`。
- 命令与结果（本机，仓库根）：
  - `.venv/bin/python -m pytest -q tests/test_scheduler_run_http.py` → 26 passed
  - `node --test tests/test_functional_scheduler.cjs` → 18 passed
  - `node --test tests/test_desktop_core_integration.cjs` → 12 passed
  - 客户端套件合并复跑：`node --test tests/test_feature_action_clients.cjs tests/test_functional_models.cjs
    tests/test_functional_scheduler.cjs tests/test_functional_context.cjs tests/test_scheduler_frontend.cjs
    tests/test_desktop_core_integration.cjs tests/test_desktop_scheduler_poll.cjs` → 101 passed / 0 failed
  - 既有调度与会话回归：`.venv/bin/python -m pytest -q tests/test_scheduler_*.py tests/test_session_*.py
    tests/test_conversation_*.py` → 340 passed / 0 failed
  - Desktop 构建：`cd desktop && npm run build`（`vite build` + `tsc -p tsconfig.main.json`）→ 通过
- 轮询修复（task 7.5，`hooks/useSchedulerNotifyPoll.ts`）：
  - 身份失效：`tenantId` / `featureRevision` / broker epoch 任一变化即重启轮询并清空 `seen` 集合，
    前一个身份的在途响应按捕获的代次判定为陈旧后丢弃；暂停或离开同样清空去重状态。
  - 同秒去重与满页续取：先按 `since` 窗口以 `offset` 分页排空（`nextDrainStep`，`MAX_PAGES` 有界，
    避免服务器恒返满页时空转），同一秒内的多条运行全部收集后再推进游标；再按 `run_id` 去重。
  - 关闭/拒绝/故障停止：动作关闭时不起请求；非瞬时错误暂停轮询而非无退避重试。
- 覆盖：403/404/409/503 与真实 HTTP 状态、分页、`history_scope`、预览与正文、删除确认与失败保留、
  关闭投影不发请求、陈旧响应丢弃。
- 真实门槛（未完成，未开放）：真实渠道运行历史的双端验收（§8.3）。


## 7. 发布门禁与真实渠道验收（tasks §8）

（未执行。真实渠道创建/执行/历史/删除、共享 Agent 双用户、跨租户、管理员与权限撤销验收
必须在具备真实测试渠道的环境执行；未完成前八个动作保持 `open={}`，`/auth/context` 报告
`not_accepted`，客户端不请求。）

### 7.0 已知失败裁定（task 8.1）

对全量回归中出现的两类失败做了「与本 change 是否相关」的独立裁定，方法为在**同一部署状态**下
`git stash` 回退本 change 的源码（`config.json` / `memory/` / `identity.db` 为 gitignore 的真实部署
状态，两轮完全一致），以及新建 `HEAD` 干净 worktree 复跑。裁定结论：**两类均非本 change 引入，
也不是真实授权缺陷**，不做功能修改，按基线问题独立处理。

| 失败 | 复现条件 | 基线结论 | 裁定 |
| --- | --- | --- | --- |
| `test_console_migration_drill.py::MenuGrantMappingDrill::test_the_chain_maps_every_role_that_held_a_legacy_id`（`9 != 8`） | 干净 worktree `f5d7d764`（无本 change 任何文件）单文件运行即失败 | 失败 | 旧菜单 / 遗留 ID 映射的既有基线问题（8.1 所述「旧前端/菜单基线问题」）。独立跟进，不阻断本 change |
| `test_external_channel_propagation.py` 4 项（外部工具、本人邮箱派发、租户传递、同事邮箱拒派） | 仅在 `tests/test_default_change_session_anchor.py` 之后运行时失败；单独运行 10 passed；`git stash` 回退本 change 后同序运行**同样 4 failed** | 失败 | 既有测试隔离缺陷（前序用例污染共享身份状态），非真实授权缺陷。8.1 要求修复的是真实授权缺陷，本项不属此列；如需修复应作为独立 change 处理 `test_default_change_session_anchor.py` 的状态清理 |

配套说明：`tests/test_web_database_capability_acceptance.py` 中本 change 八个动作的缺口断言已按
「已注册未开放」重写（`DECLARED_WHILE_CLOSED`），关闭态一律断言 503 且与
`/auth/context.feature_actions` 同一份声明，未交付的更新接口仍保留关闭检查（task 8.2 已完成）。

