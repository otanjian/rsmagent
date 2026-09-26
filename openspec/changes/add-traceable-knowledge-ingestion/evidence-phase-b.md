# 阶段 B 证据：原件保存与生命周期

本文件记录 `add-traceable-knowledge-ingestion` 阶段 B（任务 2.1–2.5）的实现与验收证据。仅记录已实际执行的检查；未实现项不在此宣称完成。上传开关在验收通过前保持默认关闭（`knowledge_source_upload_enabled=False`、`knowledge_conversion_enabled=False`）。

## 2.1 逐文件幂等上传、流式限额、staging、哈希校验、提交状态与原子落盘

实现位置：`agent/knowledge/catalog.py`（`begin_upload` / `_reserve_pending_locked` / `delete_version_record` / `delete_source_record` / `storage_usage`）、`agent/knowledge/sources.py`（`SourceAssetService.save_files` / `_save_one` / `_publish_blob` / `_discard_failed_attempt`）。

- **稳定请求标识**：每个文件使用 `request_id` + 序号 + 文件名作为 `request_key`；重试同一标识且内容相同返回同一资料／版本，内容不同返回 `request_key_conflict`。
- **决定与分配同一事务**：`begin_upload` 在单个 `BEGIN IMMEDIATE` 中完成「请求键复用 → 显式更新目标 → 同库去重 → 同名判定 → 新资料创建 → 分配 pending 版本」，避免并发重复分配版本号。
- **提交状态与原子落盘**：先写入 `pending` 版本记录，内容写 `staging/<version_id>/`，校验大小与 SHA-256 后用 `os.replace` 原子发布，最终再次校验并置 `committed`；发布失败删除 pending 记录与半成品，且新建的空资料一并回滚。
- **完整提交后才回执**：`saved` 仅在记录 `committed` 后返回；未提交版本使 `has_blocking_tasks()` 为真，阻止模式切换。
- **服务端限额**：`knowledge_source_max_files`（默认 100）、`knowledge_source_max_file_size`（默认 10 MiB）、`knowledge_source_max_batch_size`（默认 200 MiB）；批量级超限在任何写入前拒绝，单文件超限/读取超限按逐项失败返回，且不占用容量。

未提交版本读取语义：`download` 只服务 `committed` 版本；`pending`（中断残留）在重试时被重新写入并提交，或由失败路径清理。

## 2.2 同库去重、同名显式选择、不可变版本、容量预留与审计

- **同库去重**：仅对 `active` 资料按内容哈希复用（`reused`/`duplicate_content`），不生成重复可检索文档；已停用／已删除资料不参与去重，避免经同名或同哈希泄漏。
- **同名不同内容**：默认 `ask` 返回 `name_conflict`（含既有资料与 `latest_version`），`new` 新建同名列资料，`update` 显式追加版本；未显式选择不覆盖。
- **不可变版本**：`latest_version+1` 分配并记录最终路径，已提交原件不原地覆盖；携带 `expected_version` 的并发更新只有一方成功，另一方返回 `stale_version`。
- **容量预留／结算／失败释放**：新增 `storage_usage()`（committed / pending 拆分，排除 `deleted`）与 `_StorageAllowance`（`reserve`/`commit`/`release`，幂等）。预留先于 pending 记录创建，避免同一版本被计算两次；失败项释放，删除资料即时释放（不等磁盘清理）。
- **审计**：`_audit` 复用 `IdentityService.record_audit`，通过既有 sanitizer 记录 `knowledge.source.*` 主体／目标／结果，仅携带 id、数量与结果，失败不改变业务结果（best-effort）。

## 2.3 资料／版本／分类 API 与安全下载／预览，租户路由登记

新增 fork handler：`channel/web/fork/handlers/knowledge_sources.py`；路由登记于 `channel/web/route_registry.py`。

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/knowledge/sources` | GET | 资料列表 + 能力投影 + 限额 |
| `/api/knowledge/sources/detail` | GET | 资料详情（版本、任务、状态） |
| `/api/knowledge/sources/download` | GET | 按指定版本流式回读原件 |
| `/api/knowledge/sources/upload` | POST | 保存原件（multipart） |
| `/api/knowledge/sources/lifecycle` | POST | 停用／重新启用／删除 |
| `/api/knowledge/sources/task` | POST | 任务重试／取消 |

- 基础 API 与 fork handler 共用 `SourceAssetService`，限额、去重、生命周期规则不在两处重复。
- **安全下载／预览**：仅 `application/pdf`、图片（不含 SVG）、音视频与纯文本类内联预览，其余一律 `Content-Disposition: attachment`；所有响应带 `X-Content-Type-Options: nosniff` 与 `Cache-Control: private, no-store`；HTML/脚本原件不会以控制台同源身份执行。
- **归属与授权**：JSON 路由复用 `_db_scope` + `_require_read_permission("knowledge.read")` + `_require_tenant_agent_binding` + `_require_private_owner`；写路由复用 `_require_knowledge_write`（数据根 + Agent 归属）。`/api/knowledge/sources/download` 登记 `tenant_from_resource=True`，由被寻址 Agent 的绑定推导租户并重新校验成员关系，浏览器无 `X-Tenant-ID` 也能下载。

## 2.4 停用／重新启用／删除与最小串行执行器清理分支

- **停用**：事务改生命周期、增加 `revision`、取消待执行转换，转换正文立即退出有效集合（`KnowledgeScope` 基于 `catalog`，不依赖开关）。
- **重新启用**：重验证后恢复 `active`；清理任务执行前会重新核对生命周期，避免被停用／重启用后的迟到清理删除文件（`stage="skipped"`）。
- **删除**：先事务改 `deleted`、增加 `revision`、取消转换任务、清除生效资格；同一 `tasks` 表创建 `cleanup` 任务并设置目标任务；随后在同一使用锁内跑 `agent/knowledge/runner.py::process_pending_tasks` 完成物理清理。
- **清理失败可重试**：失败任务保持 `queued` 且 `stage="cleanup_failed"`，使根持续报告 busy（阻止模式切换）并可经 `/api/knowledge/sources/task` 重试；`mark_interrupted_tasks()` 后中断的清理任务被改回 `queued`，进程重启后不会被当作已完成。
- **删除清理完成不等于缓存已物理清空**：正文退出检索由生命周期与 `revision` 决定，缓存清退按 `knowledge-source-traceability` 在下次同步处理。

## 2.5 验收证据

命令：`.venv/bin/python -m pytest tests/test_knowledge_catalog.py tests/test_knowledge_scope.py tests/test_knowledge_service.py tests/test_knowledge_capabilities.py tests/test_knowledge_root_locks.py tests/test_knowledge_web.py tests/test_knowledge_sources.py tests/test_knowledge_sources_web.py tests/test_knowledge_console_database.py -q`

结果：`115 passed`。关键覆盖：

- **响应丢失重试**：相同 `request_id` + 相同内容返回同一资料／版本，不重复占用容量；相同标识不同内容返回冲突。
- **并发上传**：两线程同时上传相同内容收敛到同一资料（进程内每根上传互斥 + 事务内决定）；两线程基于同一 `expected_version` 更新时一方 `saved`、另一方 `stale_version`，版本号不重用。
- **磁盘／记录中断**：发布失败后无资料、无 pending 记录、无半成品文件；中断的清理任务重启后重新入队并完成。
- **部分失败**：批量中单文件超限仅该项失败，良好项成功且失败项不占容量。
- **删除恢复**：清理失败 → `cleanup_pending=true` 且 `has_blocking_tasks()` 为真；恢复后可重试至 `complete`，文件被移除、容量释放。
- **持锁期间切换**：上传发布期间独占切换锁不可取得（使用锁持续持有）；存在切换恢复标记时上传／下载返回 `KnowledgeUnavailableError`（HTTP 503 `knowledge_unavailable`）。
- **归属与授权（数据库模式真实 IAM）**：跨租户 Agent 列表 404；无 `X-Tenant-ID` 的下载按资源推导租户成功；他成员访问私有资料 403/404，私有 owner 本人成功；无 `knowledge.read` 的角色下载 403（撤权）；租户管理员可对共享库资料执行生命周期写。
- **超限投影与拒绝**：请求体超批量上限在身份解析前返回 `source_quota_exceeded`；上传开关关闭时读取仍可用且不创建 catalog，写入返回 `source_unavailable`。

其他校验：

- `.venv/bin/python scripts/check-route-coverage.py` → `OK`（202 routes, 247 method entries）。
- `.venv/bin/python scripts/check-web-module-seams.py` → `OK`（0 findings）。
- `openspec validate add-traceable-knowledge-ingestion --strict` → `Change 'add-traceable-knowledge-ingestion' is valid`。

## 已知边界（不宣称完成）

- **跨进程同内容并发去重**：进程内由每根上传互斥保证收敛；两个进程在同一窗口内上传相同内容时，事务可防止版本号冲突，但不保证合并为同一资料（单进程部署为当前默认；以 pending 记录保证容量与记录一致）。
- **转换产物容量计量**：当前配额统计 `versions.size`（原件）。转换产物（阶段 C）需要在任务 manifest 计入容量，接入点已由 `storage_usage()` 预留。
- **转换状态／重试／取消**：`request_conversion` 尚未实现，`retry_task` 对转换任务返回 `source_unavailable`，`cancel_task` 仅接受转换任务但当前无转换任务可取消；这些在阶段 C 完成。
- **UI**：原始资料页签、上传面板与资料详情属阶段 3，本次只提供服务与 HTTP 接口。

阶段 B 依赖（A）已通过；保存依赖已由上述测试与真实 IAM 路由验证。生产上传开放需在阶段 3 页面与阶段 4 转换完成后按阶段门槛决定。
