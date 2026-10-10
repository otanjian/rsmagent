# 平台侧切片实施记录（add-coding-agent-personal-workspace）

日期：2026-10-10。状态：**部分实施**。本记录只覆盖已在平台仓库 `rsmagent` 内实现并通过自动化验证的部分；配套 RSMCode（`/Users/jiantan/ai_assistant/rsmcode`）与真实远端联调均**未执行**。

## 1 范围与前置结论

本 change 设计为跨两个仓库（平台 + 配套 RSMCode）的一次交付，且 tasks 的 `1.2`、`4.3`、`5.1`–`5.3` 明确要求**真实远端**证据（“不以 mock 代替上线证据”）。本工作区不存在可达的 OpenCode 主机与凭据，因此这三条无法在此执行，未做任何替代性声明。

本次实施选择平台侧基础切片：目录绑定、身份解析、远端 prepare 契约、单一开关，以及把个人会话创建接入 `U`。文件面板 `coding_remote` 来源、附件落盘、结果输出、Git/搜索/SAP 适配、前端来源切换**尚未实施**（见第 4 节）。

## 2 已实现（含验证）

| 位置 | 改动 | 验证 |
| --- | --- | --- |
| `config.py`、`config-template.json` | `opencode.personal_workspace.enabled` 单一部署开关，默认 false | `tests/test_coding_settings.py` |
| `agent/coding/__init__.py` | `CodingSettings.personal_workspace_enabled`；`CODING_WORKSPACE_UNSAFE`/`CODING_WORKSPACE_CONFLICT` 稳定错误码 | 同上 |
| `auth/store.py` `_migration_49` | 受保护的 `coding_user_workspaces` 表：`(service_id, tenant_id, base_project_dir, user_id)` 与 `(service_id, effective_directory)` 唯一约束，`username_at_creation` 冻结规范拼写 | `tests/test_coding_personal_workspace.py` |
| `agent/memory/conversation_schema.py`、`conversation_store.py` | `opencode_session_links.workspace_id` 可空增量列；读写与创建路径贯通 | `tests/test_coding_session_store.py` |
| `agent/coding/workspace.py`（新增） | `CodingWorkspaceService`：按不可变 `user_id` 从身份库取规范账号名、单段安全校验、`U=P/user/<username>` 解析、绑定预留/复用/冲突、失败保留可重试的 `creating` | `tests/test_coding_personal_workspace.py`（14 例 + 子测试） |
| `agent/coding/opencode.py` | `prepare_workspace(P, U)` 远端契约（字段名按设计草案，隔离在单一方法内便于日后按配套仓库收敛） | `tests/test_opencode_client.py` |
| `agent/coding/sessions.py` | 个人模式下 `reserve` 先解析并准备 `U`，会话 `Location`、链路 `project_dir` 与 `workspace_id` 一致；关闭开关时保持原 `P` 行为 | `tests/test_coding_personal_workspace.py::PersonalSessionReserve` |

已覆盖的关键行为：同一主体重复打开复用同一绑定、不同账号得到不同目录、未知账号/不安全账号名被拒绝且不创建目录、准备失败保留 `creating` 并可用同一绑定重试、别的被占目录不被冒领、会话重试复用同一 `workspace_id`、开关关闭时不进入个人模式。

## 3 验证命令与结果

```bash
.venv/bin/python -m pytest tests/test_coding_personal_workspace.py -q -p no:randomly
# 17 passed, 13 subtests passed

.venv/bin/python -m pytest tests/test_coding_settings.py tests/test_opencode_client.py \
  tests/test_coding_session_store.py tests/test_conversation_schema_seam.py -q -p no:randomly
# 全绿

.venv/bin/python -m pytest tests/test_coding_agent_routes.py tests/test_coding_agent_type_boundary.py \
  tests/test_coding_session_lifecycle.py tests/test_coding_session_routes.py tests/test_coding_session_store.py \
  tests/test_coding_session_sync.py tests/test_coding_settings.py tests/test_coding_browser_auth.py \
  tests/test_coding_model_preferences.py tests/test_opencode_client.py tests/test_conversation_schema_seam.py -q -p no:randomly
# 219 passed

.venv/bin/python -m pytest tests/test_shared_agent_personal_workspace.py tests/test_history_agent_workspace.py \
  tests/test_workspace_user_dir.py tests/test_workspace_layout_template.py -q -p no:randomly
# 68 passed

.venv/bin/python -m pytest tests/test_agent_admin.py tests/test_coding_model_preferences.py \
  tests/test_models_handler.py tests/test_tenant_channel_instances_migration.py \
  tests/test_identity_service_writes.py -q -p no:randomly
# 149 passed, 3 subtests passed
```

## 4 未实施 / 未验证（不得视为已完成）

- **远端服务（RSMCode）**：受认证的 `prepare` 与目录归属记录、最小文件操作、附件落盘、可信配置来源适配均未实现。`prepare_workspace` 目前只有平台侧调用方与契约。
- **文件入口**：`/api/workspace/*` 的 `coding_remote` 分派、`agent.coding` 远端文件客户端、真实 capabilities 投影、`@`/上传/预览下载适配均未实现；`channel/web/fork/handlers/workspace.py` 未改动。
- **前端**：`workspace.js` 来源切换（scope epoch、迟到响应丢弃、按钮按 capabilities 禁用）未改动。
- **附件与输出**：iframe 选择/拖拽/粘贴落盘、直接消息附件校验、默认输出目录传递、生成/导出接入 `U` 均未实现。
- **会话接入剩余**：`attach`、分叉、内部子任务接入 `U`，以及“仅凭同一 Git 项目 ID 不得认领另一账号会话”的加固未实现。
- **旧会话与新会话入口**：打开旧会话时按原项目快照打开本人 `U` 并提示差异的 UI 未实现。
- **Git/SAP/配额**：`user/` 冲突预检与公共 Git 排除、SAP P/U 拆分校验与导出落点、上传配额/审计接入均未实现。
- **真实远端验收**（`1.2`、`4.3`、`5.1`–`5.3`）：未执行。

本次交付范围如实表述为“平台侧目录绑定、身份解析、单开关与新个人会话创建的实现与单元/接缝验证”，**不**代表个人文件入口、附件与输出链路或存量会话已完成。

## 5 附带修正

`tests/test_coding_settings.py::test_the_capability_is_registered_so_a_deployment_can_edit_it` 在变更前即为**既有失败**（`config.py` 已含 `browser_sso`，断言未同步）。由于本次改动同处该配置块，已将该断言更新为包含 `browser_sso` 与新增的 `personal_workspace`。这属于既有缺陷的顺带修复，单独记录，未牵动其他行为。
