# 阶段 A 证据：兼容基础

本文件记录 `add-traceable-knowledge-ingestion` 阶段 A（任务 1.1–1.6）的依赖核对结果与验收证据。仅记录已实际执行的检查；未实现项不在此宣称完成。

## 1.1 依赖核对（真实接入点）

| 依赖 | 真实位置 | 本阶段处理 |
|---|---|---|
| 知识根解析 | `common/state_dir.py::knowledge_dir`（按存在性 opt-out、共享回退）；`agent/knowledge/service.py::KnowledgeService.__init__` | 新增 `KnowledgeScope` 以同一解析结果为根；不重构 `state_dir` |
| 私有归属与写授权 | `channel/web/fork/handlers/knowledge.py::_knowledge_write_authorized`、`_require_knowledge_write`、`_require_tenant_agent_binding`、`_require_private_owner`；`agent/admin.py::_knowledge_mode_of` | 能力投影复用同一 `_knowledge_write_authorized`，不新建授权规则 |
| 扫描／写入／重建入口 | `KnowledgeService`（`list_tree`/`_scan_dir`、`rebuild_index_md`、`build_graph`、`read_file`、create/import/rename/delete/move）；`agent/memory/manager.py::MemoryManager.sync` 知识块；`agent/memory/rebuild_index.py`；CLI `cli/commands/knowledge.py`、`plugins/cow_cli/cow_cli.py` | 服务与 `MemoryManager.sync` 接入统一有效枚举；原 `/memory rebuild-index` 权限与范围不变（复用同一枚举） |
| 前端装配 | `channel/web/templates/views/knowledge.html`、`templates/modals/knowledge-dialog.html`、`static/js/views/knowledge.js` | 本阶段未改 UI；页面工作留待任务 3.x |
| IAM | `auth/service.py`（IdentityService）；权限 `knowledge.read`；写判定按数据根＋归属（`knowledge.write` 已退役） | 沿用 |
| 审计 | `IdentityService.record_audit`、`_audit_agent_action` | 沿用；上传/版本/删除审计在阶段 B 接入 |
| 容量配额 | 现有预留／结算服务（阶段 B 任务 2.2 逐项核对） | 本阶段不动；`catalog` 仅登记路径与容量字段 |
| 执行隔离 | 既有 Skill 运行隔离（阶段 C 任务 4.1/4.7 真实核对） | 本阶段不启用转换，开关默认关闭 |

未决参数（解析器版本、超时、轮询间隔、页大小）按 design D9 留待实现时确定，不影响阶段 A。

## 实现摘要

- `agent/knowledge/catalog.py`：`catalog.sqlite3` 的 `meta`/`sources`/`versions`/`tasks` 四类记录与事务；幂等初始化 `knowledge_id`、`revision` 与无冲突内部目录映射；旧 MD 原位保留。
- `agent/knowledge/scope.py`：统一有效文档枚举（普通 MD + 当前 `active_task_id` 转换正文）与受管理路径写保护（含祖先与符号链接别名）；未登记根零影响。
- `agent/knowledge/locks.py`：根外 `flock` 共享使用锁／独占切换锁；`knowledge_busy` 由持久化任务与未提交版本推导；模式切换恢复标记。
- `agent/knowledge/capabilities.py`：区分配置、权限与真实依赖的能力投影；`knowledge_source_upload_enabled`、`knowledge_conversion_enabled` 默认关闭。
- 接入：`KnowledgeService`（读/写/列/图谱 + 使用锁 + 503 不可用）、`MemoryManager.sync`、`agent/admin.py::set_knowledge_mode`（独占锁、忙判定、恢复标记、根缓存失效）、`channel/web/fork/handlers/knowledge.py`（409/503 映射）。

## 1.6 验收证据

命令：`.venv/bin/python -m pytest tests/test_knowledge_catalog.py tests/test_knowledge_scope.py tests/test_knowledge_capabilities.py tests/test_knowledge_root_locks.py -q`

结果：`32 passed`。覆盖：

- 旧知识兼容：未登记根无受管理目录、枚举不裁剪旧 MD、旧 MD 不被移动。
- 目录冲突：预留候选名被人工目录占用时选择无冲突名并记录复用。
- 写保护：移动两端、祖先目录删除/重命名、符号链接别名的 `409 managed_knowledge_path`，且无副作用。
- 跨进程竞争：子进程持共享使用锁时独占切换锁无法取得；忙任务使切换返回 `knowledge_busy` 且目录不变。
- 改名中断恢复：已写标记的中断切换在下次以幂等回放恢复到一致状态。

回归命令与结果：

- `tests/test_knowledge_service.py tests/test_knowledge_web.py tests/test_knowledge_console_database.py tests/test_agent_admin.py` → 通过。
- `tests/test_memory_*.py tests/test_personal_memory_*.py tests/test_user_personal_memory.py tests/test_tenant_read_scoping.py`（含 knowledge/memory 控制台）→ `186 passed`。
- `python scripts/check-route-coverage.py` → `OK`；`python scripts/check-web-module-seams.py` → `OK`。

阶段 A 完成，两个新开关保持关闭；进入阶段 B 前不做生产上传/转换启用。
