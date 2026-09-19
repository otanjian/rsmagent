# 来源映射：功能 → 上游符号 → fork 调用点 → 验收测试

供后续前端结构迁移复用（tasks 8.7）。上游 SHA 以 `implementation.md` 记录为准；
**只登记本 change 实际接入的符号**，不把整份上游前端迁移标记完成。

| 功能 | 上游 SHA / 符号 | fork 调用点 | 验收测试 |
| --- | --- | --- | --- |
| 动作级可用性投影 | —（新建能力，无上游对应） | `auth/capability_matrix.py::feature_action_availability`、`auth/service.py::context_for_tenant` | `tests/test_feature_action_projection.py` |
| 客户端能力门禁 | —（新建） | `channel/web/static/js/functional-capabilities.js` | `tests/test_feature_action_clients.cjs` |
| 上下文用量/压缩 | `channel/web/fork/authorization.py`（既有 `_require_session_scope`）、`bridge/agent_bridge.py::peek_agent`、`agent/protocol/agent.py::compact_context` | `channel/web/fork/handlers/context.py`（新建）、`functional-context.js`（新建） | `tests/test_session_context_scope.py`、`tests/test_context_compaction_concurrency.py` |
| 模型目录/回退链编辑 | 既有 `channel/web/fork/handlers/models.py::save_catalog` / chain | `channel/web/static/js/functional-models.js`（新建） | `tests/test_functional_models.cjs` |
| 调度实例/接收者/创建 | `channel/web/fork/handlers/scheduler.py`（既有五 handler） | `channel/web/fork/scheduler_targets.py`（新建） | `tests/test_scheduler_create_scope.py` |
| 运行归属与访问 | `agent/tools/scheduler/integration.py::_record_scheduler_run`、`authorization.py::TaskAccessService.decide` | `agent/tools/scheduler/run_repository.py`、`run_access.py`（新建） | `tests/test_scheduler_run_scope.py` |
| 运行列表/详情/删除 | 同上 | `channel/web/fork/handlers/scheduler.py`（新增三 handler）、`functional-scheduler.js`（新建） | `tests/test_scheduler_run_http.py`、`tests/test_functional_scheduler.cjs` |

## 边界

- `channel/web/api/**` 与 `channel/web/core/**` 保持上游参考提交原样，本 change 不修改。
- 新增能力全部经 fork 模块接入；`channel/web/route_registry.py` 是唯一路由/策略清单。
- 前端结构迁移（109 处裁定、退出旧单体、Web 一键更新、微信个人渠道）不在本 change 内，
  本文件不将其标记为完成。
