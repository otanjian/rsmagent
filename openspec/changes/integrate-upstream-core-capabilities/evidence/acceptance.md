# 验收证据：integrate-upstream-core-capabilities

本文件按 tasks.md 的发布门禁要求记录每批的代码 SHA、测试命令与计数、真实步骤与回退结果。
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
| `identity-session` | `tests/test_web_database_capability_acceptance.py` | 存在；本轮以真实验收矩阵复核（§8.3），未完成前不开放 |
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
- 真实门槛（未完成，未开放）：真机切换租户/登出/重连的陈旧响应丢弃演练。

## 2. P2 上下文控制

- 代码：`channel/web/fork/handlers/context.py`（用量 GET / 压缩 POST，真实 HTTP 状态与 `{status,code,message}`）、
  `channel/web/fork/authorization.py`（`_storage_agent_key`、`_owned_context_target` 精确 owner/tenant/agent 校验、
  `_require_owned_session` 收紧为同 Agent 匹配）、`channel/web/route_registry.py`（两条 session 路由先于
  `/api/sessions/(.*)` 声明）、`agent/protocol/agent.py`（`compact_context` 深拷贝 + 提交比较 + 提交后才写长期记忆）。
- 命令与结果（本机，仓库根）：
  - `.venv/bin/python -m pytest -q tests/test_session_context_scope.py` → 14 passed
  - `.venv/bin/python -m pytest -q tests/test_context_compaction_concurrency.py` → 4 passed
  - 上述两文件随本 change 全量相关集复核 → 198 passed（见 §7）
- 覆盖：非 owner、跨租户、同名会话、伪造 Agent、非法 Origin、无实时实例（`available=false` 不建实例）、
  生成中冲突、压缩期间新消息、双并发压缩、无内容可压缩。
- 客户端（task 3.5，Web）：
  - `channel/web/static/js/functional-context.js` 挂载进 `#context-usage-host`，随现有会话生命周期
    装载/卸载；`chat.html`、`console.js`、`i18n/core.js` 同步接入。
  - 命令与结果（本机，仓库根）：`node --test tests/test_functional_context.cjs` → passed
- 真实门槛（未完成，未开放）：Web 真机上的真实会话压缩演练（`session_context.usage` /
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
- 真实门槛（未完成）：平台权限入口与真机回退演练（§8.3）。

## 4. P4 调度目标与创建

- 代码：`channel/web/fork/scheduler_targets.py`（`SchedulerTargetService`：实例、可信接收者、创建白名单投影）、
  `agent/tools/scheduler/recipient_store.py`（`list(instance_ids=...)` 过滤，保持无参兼容）、
  `channel/web/fork/handlers/scheduler.py`、`channel/web/route_registry.py`（三条路由，注册未开放）。
- 命令与结果（本机，仓库根）：`.venv/bin/python -m pytest -q tests/test_scheduler_create_scope.py` → 17 passed
- 覆盖：跨租户/他人实例、伪造目标与接收者、无 Agent `use`、硬配额、非法 Origin、绑定重验、无写重试。
- 客户端：Web 创建表单在 `functional-scheduler.js`（一次点击只提交一次，网络结果不明不重试）。
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
  `channel/web/static/js/i18n/tasks-records.js`（三语言）。
- 命令与结果（本机，仓库根）：
  - `.venv/bin/python -m pytest -q tests/test_scheduler_run_http.py` → 26 passed
  - `node --test tests/test_functional_scheduler.cjs` → 18 passed
  - 客户端套件合并复跑：`node --test tests/test_feature_action_clients.cjs tests/test_functional_models.cjs
    tests/test_functional_scheduler.cjs tests/test_functional_context.cjs tests/test_scheduler_frontend.cjs`
    → 见下方 §6.1 复跑计数
  - 既有调度与会话回归：`.venv/bin/python -m pytest -q tests/test_scheduler_*.py tests/test_session_*.py
    tests/test_conversation_*.py` → 340 passed / 0 failed
- 覆盖：403/404/409/503 与真实 HTTP 状态、分页、`history_scope`、预览与正文、删除确认与失败保留、
  关闭投影不发请求、陈旧响应丢弃。
- 真实门槛（未完成，未开放）：真实渠道运行历史的 Web 验收（§8.3）。

### 6.1 客户端套件复跑计数（本 change 范围内）

- 命令：`node --test tests/test_feature_action_clients.cjs tests/test_functional_models.cjs
  tests/test_functional_scheduler.cjs tests/test_functional_context.cjs tests/test_scheduler_frontend.cjs`
- 结果：**67 passed / 0 failed**（2026-09-19 复跑）。


## 7. 发布门禁与真实渠道验收（tasks §8）

（Web 端真实门槛执行中。
真实渠道创建/执行/历史/删除、共享 Agent 双用户、跨租户、管理员与权限撤销验收
必须在具备真实测试渠道的环境执行；未完成前八个动作在主工作区保持 `open={}`，`/auth/context` 报告
`not_accepted`，客户端不请求。）

### 7.0a 真实验收中发现的缺陷（已修复，task 6.7 / 8.4 输入）

在开放声明的真实服务上做 P6 验收时发现：**全新部署的首次运行历史读取返回 500**。

- 复现（全新数据目录、八个动作已开放、已登录并带 `X-Tenant-ID`）：
  - `GET /api/scheduler/runs` → `500 {"code":"internal_error"}`（日志 `Scheduler runs list error: OperationalError`）
  - `GET /api/scheduler/runs/detail?run_id=never-ran` → `500`
  - `POST /api/scheduler/runs/delete` → `503 run_store_unavailable`
- 根因：归属表 `fork_scheduler_run_scopes` 只由**写入**路径创建（`RunScopeRepository.record` →
  `ensure_schema`）。`list_visible` / `get_visible` 既未建表也未映射查询异常，join 抛 SQLite
  `no such table` 后逃逸成 handler 的通用 500；`delete_visible` 把它映射成 503 而不是 404。
  既有测试全部通过，是因为夹具在开头就调用了 `repository.ensure_schema()`，**全新部署状态从未被覆盖**。
- 修复：`agent/tools/scheduler/run_repository.py`
  - 新增 `_ensure_readable()` / `_has_table()`：读取/删除前按需建表，先用 `sqlite_master` 廉价探测，
    稳态部署不在每次分页上重跑 DDL、不取写锁；探测本身失败仍映射 503。
  - `list_visible` / `get_visible` 把查询异常映射为 `RUN_STORE_UNAVAILABLE`/503，不再泄漏裸 500
    （「读不到」与「没有记录」不得互相冒充）。
  - `delete_visible` 在事务前调用 `_ensure_readable()`。
- 回归测试：`tests/test_scheduler_run_http.py` 新增 `_fresh_history` 夹具（**不**预建表，并断言表确实不存在）
  与 4 个用例：表确不存在、列表 200 空页且 `history_scope=attributed_only`、详情 404、删除 404。
- 验证：
  - `.venv/bin/python -m pytest -q tests/test_scheduler_run_http.py tests/test_scheduler_run_scope.py` → **67 passed**
  - 真实服务（全新数据目录）：列表 `200 {"runs":[],"history_scope":"attributed_only"}`、详情
    `404 run_not_found`、删除 `404 run_not_found`；随后归属表materialise 且第二次读取仍 200；
    日志 `internal_error` / `Scheduler runs list error` 出现次数为 0。

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
该文件在 task 8.5 之后按批次拆成两半（见 §7.3）：未验收的 R1 两键仍走**关闭**断言，
已验收的 R2 六键新增**开放**断言（`test_the_projection_agrees_with_the_open_gate`）。

## 8. 真实渠道验收记录（task 6.7 / 8.3）

在真实部署上按发布形态执行，全部读数为**真实 HTTP**（状态码 + 报文逐字留存），
HTTP 无法产生的输入（第二用户的归属运行、公共运行、无归属的遗留行）一律经
**生产仓库**（`RunScopeRepository` / `ConversationStore`）写入，从不手工插行。

- 部署：`/tmp/rdai-acc/rc-open`（八个动作声明开放的候选检出）· 端口 **9942** ·
  `COW_DATA_DIR=/tmp/rdai-acc/d-r2` · `RDAI_DISABLED_ACTIONS` 未设置。
- 复现步骤、逐条原始报文（`request` / `status` / `body` 三段）与种子状态：
  `/tmp/rdai-acc/R2-EVIDENCE.md`；脚本 `r2_roster.py` / `r2_seed.py` /
  `r2_acceptance.py`。
- 结果：**32 / 32 PASS**，覆盖
  - 6.7：新鲜部署的首次历史读取（200 空页 + `history_scope=attributed_only`，非 500）、
    未知运行的详情/删除 404、实例与可信接收者枚举、任务创建（`scope=personal`）、
    手动执行、归属记录（tenant / owner / scope / agent / task / session 六列逐字核对）；
  - 8.3：详情在持有会话授权时返回正文、可见但非本人会话时**扣留正文**、
    公共运行与遗留无归属行、共享 Agent 双用户互不可见、跨租户拒绝、
    平台管理员越权面、删除的授权与撤销（`8.3.del0-5` / `8.3.rev`）。

### 8.1 本轮验收发现的两项缺陷

| # | 缺陷 | 判定 | 处置 |
| --- | --- | --- | --- |
| 1 | `GET /api/scheduler/runs/detail` 在「扣留正文」路径上回 **HTTP 404 + `status:success`** 报文 | **本 change 引入**（fork handler 吞掉 `web.HTTPError` 后未回滚 `web.ctx.status`） | 已修（`5f6d1897`）：`channel/web/fork/handlers/scheduler.py` 对 `_scheduler_run_body` 的响应状态/头做快照并在每条退出路径复原；`tests/test_scheduler_run_http.py` 改为直连 WSGI app 断言**状态行与响应头**，不再只断言报文。本轮回跑 `8.3.d2b` → `HTTP 200`、`8.3.d2` → `HTTP 200` |
| 2 | `POST /api/scheduler/run` 带空 `agent_id` + `run_key` 时回 200 + 裸 Python 错误 | **基线缺陷**（`agent/tools/scheduler/authorization.py` 与 `source=upstream` 的路由均早于本 change；在 `f5d7d764` 干净检出上同探针同样作答） | 本 change 内**不修**，登记独立跟进：`'…'.join` 的 `agent_id` 分量缺 `or ""` 兜底 |

### 8.2 未能验证（环境限制，非结论）

- **真实渠道投递落地**：种子实例使用占位凭据（`cli_acc_r2` / `acc-r2-secret`），
  发送在提供方侧失败，运行以 `status=error, error='deferred or delivery failed'` 落库。
  执行的**记录、归属与历史**已端到端验证；消息真正送达真实飞书租户 **NOT RUN**（缺真实凭据）。
- **旧版本二进制写出的遗留行**：本轮遗留行由本版 `ConversationStore.create_run` 不写归属快照产生，
  形状与迁移前一致；由更早二进制写出的行 **NOT RUN**。

## 9. 干净隔离检出完整回归与失败裁定（task 8.4）

```bash
git worktree add --detach /tmp/rdai-acc/rc-final 74be49dd      # 交付提交，非工作区
cd /tmp/rdai-acc/rc-final && .venv/bin/python -m pytest -q tests/
```

| 检出 | 提交 | 结果 |
| --- | --- | --- |
| RC 冻结（含缺陷 1 修复，六动作仍关闭） | `5f6d1897` | 27 failed, 5896 passed, 31 skipped, 416 subtests passed (820 s) |
| **交付提交** | `74be49dd` | **27 failed, 5899 passed, 31 skipped, 416 subtests passed** (1408 s) |

两者相差的 3 项 passed 正是 task 8.5 新增的部署关停开关用例
（`test_the_deployment_switch_closes_an_accepted_batch` /
`test_the_deployment_switch_does_not_reach_the_handler` /
`test_a_sibling_action_stays_served_while_its_neighbour_is_closed`），
失败数**未变**。

27 项失败全部落在与本次改动**无路径交集**的两族，且逐一取下基线对照：

| 失败族 | 计数 | 基线对照（`f5d7d764` 干净检出，无本 change 任何文件） | 裁定 |
| --- | --- | --- | --- |
| `test_weixin_qr_flow.py` 22 + `test_console_migration_drill.py` 1 + `test_external_connection_service.py` 1 + `test_external_connections_api.py` 1 + `test_personal_console_frontend.py` 1 | 26 | 同 5 个文件、同一检出：**26 failed, 88 passed**，失败集合**逐项完全一致**（`diff` 为空） | **预存在**。失败报文均为 `channel type is not available for tenant configuration` / `channel type is not open for personal access`，即 `channel/channel_instances.py::inbound_identity_admissible` 对**未打身份戳**渠道类型（微信/Telegram/Slack/Discord）的**设计性拒绝**；五个文件均存在于基线且本 change 未改动其一（`git diff` 为空） |
| `test_scene_skills.py::SceneMigrationTests::test_original_files_match_recorded_source_hashes` | 1 | 同用例在 `f5d7d764` 新鲜检出上同样 **SUBFAILED** | **预存在**。source-manifest 记录哈希与**新鲜检出**的文件集不匹配（8.1 所述「旧 source-manifest 基线问题」）；工作区（含未跟踪的 scene 内容）运行该用例是通过的。两处失败指向的条目名不同（交付检出 `sapdataanalysis/…`、基线检出 `sap_data_analysis/…`），因为这正是 glob 在该检出下**实际找不到**的文件，属检出形态差异而非行为回归 |

**裁定结论**：27 项失败**无一由本 change 引入**；两族均非真实授权缺陷（前者是部署形态下的能力缺席，
后者是 manifest/检出形态），按 8.1 的既定口径独立跟进。
**新增能力失败数 = 0，授权失败数 = 0 → 不阻断发布。**

## 10. 声明批量开放与部署 deny 键（task 8.5）

### 10.1 批次状态

`auth/capability_matrix.py` 的八个 slice 按「实现 + 真实验收入口验收」分别判定，
本 change 的批次边界在代码注释与测试中同时固化：

| 批次 | slice | 声明 |
| --- | --- | --- |
| **R2 已验收** | `scheduler_instances` / `scheduler_recipients` / `scheduler_create` / `scheduler_runs_list` / `scheduler_runs_detail` / `scheduler_runs_delete` | `accepted=True`，`open={动作: ACCESS_READ/EXECUTE}` |
| **R1 待验收** | `session_context_usage` / `session_context_compact` | `accepted=False`，`open={}` |

`tests/test_feature_action_projection.py` 用 `ACCEPTED` / `UNACCEPTED` 两个集合钉住该边界：
投影、路由门禁两个方向都对着**同一份声明**断言，并新增
`test_the_batch_state_covers_every_action_exactly_once` 防止两集合漏项或重叠。

### 10.2 前端投影与路由一致（真实服务，`/tmp/rdai-acc/drill_85.py`）

交付提交 `74be49dd`、端口 **9943**、`COW_DATA_DIR=/tmp/rdai-acc/d-85`。

**阶段一（部署持有两个 deny 键 `scheduler.runs.list,scheduler.runs.delete`）**：

```
action                   projection                     gate  gate body
scheduler.instances      True/-                         200
scheduler.recipients     True/-                         200
scheduler.create         True/-                         400   invalid_request
scheduler.runs.list      False/disabled_by_deployment   503
scheduler.runs.detail    True/-                         404   run_not_found
scheduler.runs.delete    False/disabled_by_deployment   503
session_context.usage    False/not_accepted             503
session_context.compact  False/not_accepted             503
8/8 routes agree with the projection
```

**阶段二（移除 deny 键并重启）**：两个被关闭的动作回到 `True/-`，
`scheduler.runs.list` → `200 {"runs":[],"history_scope":"attributed_only"}`（**新鲜部署首次读取**）、
`scheduler.runs.delete` → `400 run_id required`；R1 两键仍为 `not_accepted` + 503；
再次 **8/8 一致**。

补充核对：

- **deny 键只减不增**：`RDAI_DISABLED_ACTIONS` 只能关闭本 build 已服务且**已 accepted** 的动作，
  未 accepted 的动作报 `not_accepted` 而非 `disabled_by_deployment`（两阶段均可见）；
- **拼错即拒启动**：`parse_disabled_actions('scheduler.runs.list,bogus.key')` →
  `CapabilityConfigurationError: unknown RDAI_DISABLED_ACTIONS entries: bogus.key`
  （关停开关是事故响应工具，错键不得静默 no-op）；
- **客户端资产实际可取**：`GET /assets/js/functional-capabilities.js` → `200`、5229 字节，
  模块内含 `feature_actions` / `disabled_by_deployment` / `not_accepted` 三个原因分支；
- **客户端一致性套件**：`node --test tests/test_feature_action_clients.cjs` → **11 passed**。

### 10.3 关停开关的回归保护

`tests/_helpers.py` 新增 `close_capability_actions(...)`：模拟运维用 deny 键关闭**已 accepted** 的动作，
保持 `implemented` / `accepted` 不变。`tests/test_scheduler_run_http.py` 据此新增
`test_the_deployment_switch_closes_an_accepted_batch`、
`test_the_deployment_switch_does_not_reach_the_handler`、
`test_a_sibling_action_stays_served_while_its_neighbour_is_closed`，
把「关停只关目标、不误伤同批邻居」钉死在测试里，而不是留给部署时人工观察。

## 11. 先关闭再代码回退演练与版本记录（task 8.6）

同一部署（`COW_DATA_DIR=/tmp/rdai-acc/d-85`、端口 9943）四阶段演练，每阶段读真实 HTTP，
磁盘状态直接读数据目录（不经过本 change 的代码）：

| 阶段 | 运行代码 | 调度六动作 | R1 两动作 | 既有数据 |
| --- | --- | --- | --- | --- |
| **open** | 交付 `74be49dd`，无 deny 键 | 投影 `True/-`，路由 200/400/404 | `not_accepted` + 503 | 播种 + 真实 `POST /api/scheduler/create` 建任务、`POST /api/scheduler/run` 触发执行 |
| **closed** | 交付 `74be49dd` + 六个 deny 键 | 投影 `disabled_by_deployment`，**六路全 503** | `not_accepted` + 503 | 逐项不变 |
| **base**（代码回退） | 基线 `f5d7d764` 同一数据目录 | 新路由 **404**（旧二进制没有这些入口）；`/auth/context` 无 `feature_actions`（旧投影不含该字段） | 405（同日路径不同方法） | 逐项不变；旧二进制**正常启动并读取**同一份数据 |
| **restored** | 交付 `74be49dd` 再次启动 | 投影回到 `True/-`，`GET /api/scheduler/runs` **仍列出回退前那两条运行** | `not_accepted` + 503 | 逐项不变 |

四阶段逐项不变的数据（关键项逐字）：

```
attribution_rows            2        # fork_scheduler_run_scopes 两行，六列归属完整
attribution                 [('91ca0290…', 'tnt_XwPN…', 'usr_FqiN…', 'personal', 'default', 'd3b30bcc…', 'acc86-sched-session'),
                             ('e36af1bf…', 'tnt_XwPN…', 'usr_FqiN…', 'personal', 'default', '40fb1e44…', 'acc86-sched-session')]
ledger_tables               ['fork_scheduler_run_scopes', 'messages', 'runs', 'sessions', 'sqlite_sequence']
session_row                 ('acc86-session', 'web', 'usr_FqiN…', 'tnt_XwPN…', 2)
session_messages            2
task_count                  4        # 四条 personal 任务（tasks.json）
recipients_kept             True     # acc86-receiver
catalog_overrides           ['acc86-model']   # system/models.json
instance_rows              1        # tenant_channel_instances
```

结论：

1. **扩展表保留**：`fork_scheduler_run_scopes` 在关停、代码回退、再次升级三个往返中**始终存在且行数不变**；
   基线二进制对它无感知（不读、不删、不校验），因此**先升级后回退不产生孤儿或破坏**。
2. **既有数据可用**：任务、会话（含消息体）、模型目录、接收者、渠道实例在四阶段中一致，
   且每一阶段都能通过当时的**真实 HTTP 面**读到（`/api/sessions` 在 closed/base/restored 均 200 且
   命中 `acc86-session`）。
3. **扩展表按需创建**：回退前由**读路径**（`RunScopeRepository._ensure_readable`）在首次读取时物化，
   随后第二次读取仍 200 且不增行（见 §7.0a 与 6.7.2b），因此「谁先读」不影响能否升级。

### 版本与恢复记录

| 角色 | 提交 | 说明 |
| --- | --- | --- |
| 实施基线 | `f5d7d764` | task 1.1/1.2 记录的起点；本演练的**代码回退目标** |
| RC 冻结（含缺陷 1 修复） | `5f6d1897` | 六动作仍 `open={}`、`accepted=False`；全量回归在此提交记录 |
| 交付（task 8.5 批次开放） | `74be49dd` | 六动作 `accepted=True` + `open`；R1 两键保持关闭 |

**恢复口径**：若要回退到 `f5d7d764`，只需把代码切回该提交并重启，**无需回滚数据**——
扩展表与 `scheduler/tasks.json`、`system/models.json`、`memory/long-term/index.db` 均为**新增或兼容写入**，
旧二进制不读扩展表即可正常服务；再次升级到 `74be49dd` 时历史运行与归属自动可见（本演练 `restored` 阶段实测）。
若事故中需要先止血，优先用 `RDAI_DISABLED_ACTIONS` 关闭六个动作并重启
（§10 两阶段实测：投影与门禁同步收窄，数据零改动），**不必回退代码**。

## 12. 历史功能关闭时的真实执行与扩展表（task 6.7）

同一部署（`/tmp/rdai-acc/d-85`、端口 9943）以**六个动作全部 deny** 的形态启动，
证明「记录/归属」与「路由门禁」相互独立：

```
before: scope rows = 2
history route while closed: HTTP 503 database_unavailable
manual run while closed: HTTP 200 {"status": "success",
    "message": "Task '01d7107eff964e67ae582c53605dd2fb' queued for immediate execution"}
after: scope rows = 3
   ('3130c535…', 'tnt_XwPN…', 'usr_FqiN…', 'personal', 'default', '01d7107e…', 'acc86-sched-session')
newest row attributed to task 01d7107eff964e67ae582c53605dd2fb: True
history route still closed: HTTP 503
```

要点：

- 历史三路在关闭态**一律 503**（投影同时报 `disabled_by_deployment`），而**执行与归属照常写入**：
  关停的是读取面，不是记录面，因此事故期关闭历史不会丢归属证据。
- **删掉再建不重投**：记录只发生在执行时，且 `record` 不可改写；关闭/重开十数次不影响既有行数。

**建表重复执行证据**（幂等，非本任务新增，但为 6.7 所要求）：

- 全新部署首次读取：表**不存在**（`6.7.2a`：`has fork_scheduler_run_scopes=False`）→
  第一次 `GET /api/scheduler/runs` 200 空页（`6.7.1a`）→ 表被物化且 `rows=0`（`6.7.2b`）→
  第二次读取仍 200 且 `rows` 不增（`6.7.5`）；实现为 `sqlite_master` 廉价探测后按需 DDL，
  稳态部署不在每次分页上重跑建表（`agent/tools/scheduler/run_repository.py::_ensure_readable`）。
- **旧版本忽略扩展表**：§11 的 `base` 阶段，基线 `f5d7d764` 二进制在同一数据目录正常启动并服务，
  对 `fork_scheduler_run_scopes` 既不读取也不修改，两行归属在升级回 `74be49dd` 后原样可见。

