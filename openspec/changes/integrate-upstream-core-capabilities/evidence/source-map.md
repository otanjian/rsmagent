# 来源映射：功能 → 上游符号 → fork 调用点 → 验收测试

供后续前端结构迁移复用（tasks 8.7）。上游 SHA 以 `implementation.md` 记录为准；
**只登记本 change 实际接入的符号**，不把整份上游前端迁移标记完成。

| 功能 | 上游 SHA / 符号 | fork 调用点 | 验收测试 |
| --- | --- | --- | --- |
| 动作级可用性投影 | —（新建能力，无上游对应） | `auth/capability_matrix.py::feature_action_availability`、`auth/service.py::context_for_tenant` | `tests/test_feature_action_projection.py` |
| 客户端能力门禁 | —（新建） | `channel/web/static/js/functional-capabilities.js` | `tests/test_feature_action_clients.cjs` |
| 上下文用量/压缩 | `channel/web/fork/authorization.py`（既有 `_require_session_scope`；新增 `_owned_context_target` 复用其租户绑定/可见性/owner 探针）、`bridge/agent_bridge.py::peek_agent`、`agent/protocol/agent.py::compact_context`；归属前提由 `channel/web/fork/handlers/chat.py::_authorize_chat_session` 在**认领时**落 `tenant_id` 保证（见 `implementation.md`） | `channel/web/fork/handlers/context.py`（新建）、`functional-context.js`（新建） | `tests/test_session_context_scope.py`、`tests/test_context_compaction_concurrency.py`、`tests/test_chat_identity_context.py` |
| 模型目录/回退链编辑 | 既有 `channel/web/fork/handlers/models.py::save_catalog` / chain | `channel/web/static/js/functional-models.js`（新建） | `tests/test_functional_models.cjs` |
| 调度实例/接收者/创建 | `channel/web/fork/handlers/scheduler.py`（既有五 handler） | `channel/web/fork/scheduler_targets.py`（新建） | `tests/test_scheduler_create_scope.py` |
| 运行归属与访问 | `agent/tools/scheduler/integration.py::_record_scheduler_run`、`authorization.py::TaskAccessService.decide` | `agent/tools/scheduler/run_repository.py`、`run_access.py`（新建） | `tests/test_scheduler_run_scope.py` |
| 运行列表/详情/删除 | 同上 | `channel/web/fork/handlers/scheduler.py`（新增三 handler）、`functional-scheduler.js`（新建） | `tests/test_scheduler_run_http.py`、`tests/test_functional_scheduler.cjs` |

## 交付批次状态（task 8.5 定格；复核时以此为准）

`auth/capability_matrix.py` 的八个动作键分两批，批次边界由
`tests/test_feature_action_projection.py` 的 `ACCEPTED` / `UNACCEPTED` 集合钉住：

| 动作键 | slice | 批次 | 声明 | 门禁回答 | 证据 |
| --- | --- | --- | --- | --- | --- |
| `scheduler.instances` | `scheduler_instances` | **R2 已验收** | `accepted=True`，`open={read: ACCESS_READ}` | 已放行（未开放时为 503） | `evidence/acceptance.md` §8 / §11 |
| `scheduler.recipients` | `scheduler_recipients` | R2 已验收 | `accepted=True`，`open={read: ACCESS_READ}` | 已放行 | 同上 |
| `scheduler.create` | `scheduler_create` | R2 已验收 | `accepted=True`，`open={execute: ACCESS_EXECUTE}` | 已放行 | 同上 |
| `scheduler.runs.list` | `scheduler_runs_list` | R2 已验收 | `accepted=True`，`open={list: ACCESS_READ}` | 已放行 | 同上 + §12 |
| `scheduler.runs.detail` | `scheduler_runs_detail` | R2 已验收 | `accepted=True`，`open={detail: ACCESS_READ}` | 已放行 | 同上 |
| `scheduler.runs.delete` | `scheduler_runs_delete` | R2 已验收 | `accepted=True`，`open={delete: ACCESS_EXECUTE}` | 已放行 | 同上 |
| `session_context.usage` | `session_context_usage` | **R1 待验收** | `accepted=False`，`open={}` | `not_accepted` → 503 | `evidence/acceptance.md` §13（真实验收已执行：30 项 25 PASS / 5 FAIL 跨三缺陷；DEF-1 已修，复测未回全绿） |
| `session_context.compact` | `session_context_compact` | **R1 待验收** | `accepted=False`，`open={}` | `not_accepted` → 503 | 同上 |

任何部署都可以用 `RDAI_DISABLED_ACTIONS=<逗号分隔动作键>` **只关不开**已 accepted 的动作；
拼写未知键在启动时即拒绝（`CapabilityConfigurationError`），因此不存在「关错了却静默放行」。
前端迁移时**不要**从 `open` 推断能力是否存在：能力存在与否看 `implemented`，
能否调用看 `/auth/context.feature_actions` 的 `available` + `reason`
（`not_implemented` / `not_accepted` / `disabled_by_deployment`，判定优先级见
`auth/capability_matrix.py::_unavailable_reason` 与 `evidence/acceptance.md` §10.2）。

## 后续前端迁移记录（不在本 change 内）

- **R1 上下文控制的前端开放**：`functional-context.js` 与 `functional-capabilities.js` 已交付，
  但 `session_context.usage` / `session_context.compact` 两个动作在声明层仍关闭，
  等 task 3.6 / 4.5 的真实会话验收后再执行 8.5 的第二批翻转（改 `auth/capability_matrix.py`
  与 `tests/test_feature_action_projection.py` 的 `ACCEPTED` 集合，两处必须同批）。
- **Desktop 客户端**：`channel/web/fork/handlers/pages.py` 的发现式静态资产登记与
  `functional-*.js` 三模块对 Desktop 侧复用是**留待独立变更**的范围，本 change 不含 Desktop
  代码与其测试（已按用户指示移出）；`channel/web/static/js/console.js` 的能力门禁读取点可供其直接复用。
- **前端结构迁移（109 处裁定、退出旧单体、Web 一键更新、微信个人渠道）**：仍按
  `openspec/specs/web-console-frontend-modules/spec.md` 与 `web-console-module-seams/spec.md` 独立推进，
  本文件只登记本 change 实际接入的符号。

## 边界

- `channel/web/api/**` 与 `channel/web/core/**` 保持上游参考提交原样，本 change 不修改。
- 新增能力全部经 fork 模块接入；`channel/web/route_registry.py` 是唯一路由/策略清单。
- 前端结构迁移（109 处裁定、退出旧单体、Web 一键更新、微信个人渠道）不在本 change 内，
  本文件不将其标记为完成。
