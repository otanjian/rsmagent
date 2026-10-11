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

## 6 增量（后续续做）

日期：2026-10-10（续）。状态：**平台侧部分实施**。在上一版平台切片之上，补齐了个人会话的 **attach/分叉绑定继承** 与 **“同一 Git 项目 ID 不得认领另一账号会话”** 的加固，并补充账号/会话切换的针对性测试。远端服务与真实联调仍未执行。

### 6.1 已实现（含验证）

| 位置 | 改动 | 验证 |
| --- | --- | --- |
| `agent/coding/sessions.py::attach` | 当来源会话为个人会话（`workspace_id` 非空）时，被认领会话的远端目录必须等于绑定 `U`，或等于来源会话自身在服务端报告的目录（同一服务的拼写帧，覆盖符号链接/解析差异）；否则以 `coding_project_mismatch` 拒绝。个人分支**不再**使用 Git `project_id` 单独判定归属。 | `tests/test_coding_personal_workspace.py::PersonalSessionAttach`（6 例） |
| `agent/coding/sessions.py::attach` | 新链接沿用来源会话的 `workspace_id` 与 `project_dir`，使分叉后的会话继续使用同一账号目录，而非回落项目根。 | 同上 |
| `agent/coding/sessions.py::attach` | 来源为旧会话（`workspace_id` 为空）时保持原行为：按 Git 项目 ID/目录判定并登记在 `P`。 | `test_a_legacy_source_still_registers_against_the_project_root` |
| `Scene/sap_workbench/backend/store.py` | 绑定快照拆分 `project`(P) 与 `runtime_dir`(会话运行目录)；`attach_coding_session` 用 `runtime_dir`（旧行回退 `project`）判归属。 | `tests/test_sap_workbench_runtime.py::test_attach_matches_the_runtime_directory_not_the_base_project`、`::test_a_binding_without_runtime_dir_keeps_its_project_behaviour` |
| `Scene/sap_workbench/backend/runtime.py`、`http.py` | `open_workbench_runtime` 透传 `runtime_dir`；`http.py::_runtime_directory` 在个人开关启用时经 `CodingWorkspaceService` 解析 `U`，否则用 `P`，解析失败报 `coding_workspace_unavailable` 不回落。 | `tests/test_sap_workbench_runtime.py::test_runtime_directory_follows_the_personal_switch` |

覆盖的关键行为：个人来源的分叉在账号目录内被接受且继承绑定；同一 Git 项目 ID 但在项目根 `P` 或另一账号 `U` 的会话被拒绝；无目录证据的会话被拒绝（不能仅凭项目 ID 认领）；服务端以符号链接拼写一致报告的会话被接受；旧来源仍按项目根登记。

### 6.2 新增针对性测试

- 同账号多会话共用同一 `U` 与绑定（`test_two_sessions_of_one_account_share_one_directory_and_binding`）。
- 两账号在同一项目不共享会话或目录（`test_two_accounts_never_share_a_session_or_a_directory`）。
- 关闭新增开关不改写已有个人会话的 `U`（`test_turning_the_switch_off_does_not_move_an_existing_session`）。

### 6.3 验证命令与结果

```bash
python -m pytest tests/test_coding_personal_workspace.py -q -p no:randomly
# 26 passed, 13 subtests passed

python -m pytest tests/test_sap_workbench_runtime.py -q -p no:randomly
# 35 passed

python -m pytest tests/test_sap_workbench_page_read.py tests/test_sap_workbench_native_embed.py \
  tests/test_sap_workbench_access.py tests/test_coding_session_routes.py -q -p no:randomly
# 92 passed

python -m pytest tests/test_coding_personal_workspace.py tests/test_coding_session_store.py \
  tests/test_coding_settings.py tests/test_opencode_client.py tests/test_coding_session_routes.py \
  tests/test_coding_agent_routes.py tests/test_coding_agent_type_boundary.py \
  tests/test_coding_session_lifecycle.py tests/test_coding_session_sync.py \
  tests/test_coding_browser_auth.py tests/test_coding_model_preferences.py \
  tests/test_conversation_schema_seam.py -q -p no:randomly
# 245 passed, 13 subtests passed

# SAP 全套（含 screen 引擎用例）：297 passed，仅 2 例既有端口占用失败（见 6.5）
```
```

### 6.4 本增量未实施 / 未验证

- 文件入口 `coding_remote`、附件落盘、输出目录、Git 排除与前端来源切换仍未实施（同第 4 节）。
- 跨租户、越权文件引用、撤权后的文件/事件访问等 4.3 其余项需真实链路。
- 4.2 剩余：配置发现从 U 正确读取 P 的实测、SAP 导出落点接入 U，以及真实远端/浏览器联调。
- 真实远端验收（`1.2`、`4.3`、`5.1`–`5.3`）仍未执行。

### 6.5 既有失败（与本次无关）

`tests/test_sap_workbench.py::test_platform_agent_routing_metadata_is_ignored` 与 `::test_session_rejects_invalid_request_key_and_password` 在本工作区**变更前即失败**：屏幕引擎网关绑定固定端口 9911 时报 `OSError 10048`（端口被占用）。已用 `git stash` 去除本次改动后复现同样失败，确认为环境问题，非本次回归。

## 7 真实远端核对（task 1.2）

日期：2026-10-11。状态：**已执行**。上一版记录中“本工作区不存在可达的 OpenCode 主机与凭据”的判断已不成立：配套 RSMCode 就位于本机 `C:\rdai\rsmCode\opencode`，并且**有一个真实 OpenCode 服务正在运行**，可通过平台同一凭据访问。

### 7.1 真实服务

- 源码：`C:\rdai\rsmCode\opencode`（bun monorepo，OpenCode 的 RSM 分支）。
- 运行进程：`bun run --cwd packages/opencode src/index.ts serve`，监听 `127.0.0.1:4096`（即平台 `opencode.api_url`）。
- 认证：与平台同一 Basic 凭据（`opencode` / `RSM_OPENCODE_PASSWORD`）。未认证请求返回 `401`。
- 接口清单：`GET http://127.0.0.1:4096/doc`（OpenAPI）。

### 7.2 实测结论（临时 P/U，用后删除）

在 `C:\tmp\rsm_verify_<rand>\P` 建临时 `P`，`U = P\user\ZhangSan`，验证后删除。全部为读操作或单次建会话。

| 操作 | 真实结果 |
| --- | --- |
| `GET /api/location?location[directory]=U` | `200`，`{directory: U, project:{id:"global",directory:"C:\\"}}`（不在 Git 内即 `global` 项目） |
| `GET /api/fs/list?location[directory]=U` | `200`，`{location, data:[{path:"uploads\\",type:"directory"},{path:"outputs\\",type:"directory"}]}`；`path` 相对 `U`，目录带尾分隔符 |
| `GET /api/fs/list?...&path=uploads/` | `200`，`[{path:"uploads\\hello.txt",type:"file"}]`（嵌套相对路径） |
| `GET /api/fs/find?...&query=hello` | `200`，`[{path:"uploads/hello.txt",type:"file"}]`（注意此处用 `/` 分隔，与 `list` 的 `\` 不一致） |
| `GET /api/fs/read/uploads%2Fhello.txt?location[directory]=U` | `200`，原始字节，`Content-Type: text/plain` |
| 越界 `GET /api/fs/read/..%2F..%2Fproject-config.txt?...` | `500`（服务端 `Path escapes the location` 直接 die；**不**越界，但错误是 500 而非干净 4xx） |
| `POST /api/session {location:{directory:U}}` | `200`，会话 `location.directory == U`（该路由**不会**创建不存在的目录） |
| `GET /api/skill?location[directory]=U` | `200`，技能在该 Location 解析 |
| `GET /experimental/tool/ids?location[directory]=U` | `200`，工具集：`bash,read,glob,grep,edit,write,task,webfetch,todowrite,websearch,skill,apply_patch,…` |

### 7.3 关键缺口：远端没有 prepare / mkdir

- 平台 `agent/coding/opencode.py::prepare_workspace` 调用的 `POST /api/coding/workspace/prepare` 在本机真实服务上返回 `403`（路由不存在）；`/doc` 中也没有任何“创建目录 / 归属记录”的路由。
- 实测确认：`POST /api/session` 到一个**不存在**的目录不会创建目录，随后 `GET /api/fs/list` 对该目录返回 `500`。即 **U 必须事先存在**，而当前远端没有任何受认证入口可以创建它。
- 因此 task `2.2`（“在现有远端服务实现受认证的 prepare 和最小归属记录”）是当前的**硬前置**：在它落地前，平台侧 `CodingWorkspaceService.open()` 的远端 prepare 无真实实现，个人目录无法被创建，`/api/fs/*` 也无对象可列。
- **更新（2026-10-11）**：该硬前置已在 RSMCode 远端落地并通过真实联调，见第 8 节；本小节的缺口已关闭。

### 7.4 对设计的含义

真实远端已经提供了设计预期的**只读、按 Location 限定**的文件原语（`/api/location`、`/api/fs/list`、`/api/fs/find`、`/api/fs/read/*`），平台侧 `coding_remote` 的“元信息、列举、搜索、读取/下载”可直接映射，无需另造文件服务。但：

1. **Location 不是账户边界**：`/api/fs/list?location[directory]=P` 与 `=U` 都返回 `200`，服务端不按账号过滤。平台必须**始终**把 location 固定为已核验的 `U`，绝不接受客户端传入的 directory（与设计 3 节“客户端只提交对象标识和相对路径”一致）。
2. **prepare 必须落在远端**：设计明确禁止平台本地 `mkdir` 同名路径；真实的远端当前没有该能力。
3. **分隔符不一致**：`list` 用宿主分隔符（Windows 为 `\`），`find` 用 `/`。平台 DTO 需要统一归一化。
4. **越界是 500**：远端把逃逸当作内部错误。平台应在映射层把它翻译成稳定的“不存在/越权”，不要泄露为 502。

## 8 RSMCode 远端 prepare 实现与真实联调（task 2.2）

日期：2026-10-11。状态：**已实现并通过真实联调**。按确认范围修改了配套仓库 `C:\rdai\rsmCode\opencode`，并重启运行中的 4096 服务以加载新端点。

### 8.1 远端改动

| 位置 | 改动 |
| --- | --- |
| `packages/protocol/src/groups/coding-workspace.ts`（新增） | 协议组 `server.coding-workspace` 与路由 `POST /api/coding/workspace/prepare` |
| `packages/protocol/src/api.ts` | 把 `CodingWorkspaceGroup` 并入默认 server HttpApi |
| `packages/server/src/handlers/coding-workspace.ts`（新增） | `prepareWorkspace` 实现：串行化、布局强校验、归属记录、链接边界 |
| `packages/server/src/handlers.ts` | 注册 `CodingWorkspaceHandler` 到合并 handlers 层 |
| `packages/opencode/test/server/httpapi-exercise/index.ts` | 6 个 prepare 场景（首次/重试/冲突/未归属/布局/链接） |

契约与设计一致：请求 `{ base_project_dir, effective_directory, workspace_id }`，响应 `{ directory, workspace_id, created, state:"ready" }`。实现要点：

- **串行化**：整个判定+落盘在 `withLock` 内，避免并发创建竞态。
- **布局强校验**：`effective_directory` 必须严格等于 `<base_project_dir>/user/<单段安全账号>`，否则 `400`。
- **归属记录**：最小记录写入 `Global.Path.state/coding-workspace-ownership.json`（`workspace_id -> {directory, created_ms}`）。
- **冒领/冲突**：同一绑定在另一目录、已被别的绑定占用的目录、由别的绑定占用且非空的未记录目录，均以 `409` 拒绝；同一绑定同一目录重试幂等（`created:false`）。
- **链接边界**：落盘前用 `realpath` 校验 `P/user` 与 `U` 的规范化目标都必须落在项目内，且 `U` 的父目录就是 `P/user`、末段名与账号段一致；符号链接容器/末段导致的外逃以 `409` 拒绝（Windows 用 junction 复现）。
- **失败语义**：仅记录/建目录在锁内；真实磁盘错误映射为 `UnknownError`（`500`），平台侧保留 `creating` 可重试。
- **认证**：与其它受保护路由同一 Basic 凭据；未认证 `401`。

### 8.2 真实联调（平台代码 -> 127.0.0.1:4096）

重启后同机 4096 服务加载成功，`/doc` 含 `/api/coding/workspace/prepare`。用平台自身的 `OpenCodeClient` 直连真实远端（临时 `P`，用后删除）：

| 步骤 | 真实结果 |
| --- | --- |
| `GET /doc` 含 prepare 路由 | `200`，`True` |
| 平台 `OpenCodeClient.prepare_workspace(P, U, wid)` | `{directory: P\user\LiveTest, workspace_id: wid, created: true, state: "ready"}`，宿主上 `U` 真实存在 |
| 同一绑定重试 | `created: false`（幂等） |
| 远端 `GET /api/fs/list?location[directory]=U` | `200`，`location.directory == U` |

加入链接边界加固后，用项目自带 `C:\rdai\scripts\start-opencode-web.ps1 -Restart -SkipMcp` 重启（API PID 36020、WEB PID 35400 均 `OK`），再复测：`/doc` 仍含路由；`prepare_workspace` 建目录成功、重试 `created:false`；把 `P/user` 造成指向项目外的符号链接后再 prepare，远端返回 `409` `{"_tag":"ConflictError","message":"the user container resolves outside the project"}`，确认外逃被拒。

### 8.3 验证命令与结果

```bash
# 远端
bun x tsgo --noEmit -p packages/server/tsconfig.json      # 通过
bun x tsgo --noEmit -p packages/opencode/tsconfig.json    # 通过
bun run script/httpapi-exercise.ts --mode coverage        # pass=214 fail=0 skip=0 missing=0
bun run script/httpapi-exercise.ts --mode auth --fail-on-missing --fail-on-skip  # pass=214 fail=0
bun run script/httpapi-exercise.ts --mode effect --fail-on-missing --fail-on-skip # pass=212 fail=2
# effect 模式 2 例失败均为既有环境问题（见下），与本改动无关

# 平台
python -m pytest tests/test_opencode_client.py tests/test_coding_personal_workspace.py -q
# 53 passed, 13 subtests passed
```

`v2.codingWorkspace.*` 六个场景（首次、重试、冲突、未归属、布局、链接）在 coverage 与 auth 两种模式下全部 `PASS`。

**effect 模式既有失败（与本次无关）**：`pty.create` 与 `v2.pty.create` 在本机返回 `500`（`UnknownError`，创建真实 PTY 失败）。已用 `git stash` 暂存本次源码改动后重跑同样的 effect 场景，结果完全一致（`pass=1 fail=2`），确认为环境问题，非本次回归；且本改动与 PTY 无耦合，coverage/auth 模式下 PTY 场景均通过。

### 8.4 仍未实施

- `coding_remote` 文件入口的**写入/删除/回收站**、附件落盘、输出目录、前端来源切换仍未实施（读路径见第 9 节）。
- 4.2 剩余（配置发现从 U 读取 P 的实测、SAP 导出落点）与 4.3 越权/撤权项仍待真实链路。

## 9 远端只读文件入口（task 3.1 平台侧读切片）

日期：2026-10-11。状态：**已实现并通过真实联调**。个人编码会话的文件面板不再回落平台本地工作区：改由远端 `U` 提供，平台只做 DTO 适配与边界校验。

### 9.1 平台改动

| 位置 | 改动 |
| --- | --- |
| `agent/coding/opencode.py` | 新增只读文件原语：`location`、`list_files`、`find_files`、`read_file`（均以 `location[directory]` 固定 `U`） |
| `agent/coding/files.py`（新增） | `CodingRemoteFiles`：把远端 `{path,type}` 行折叠成面板契约；`RemoteSource`：`kind="coding_remote"` 的来源描述 |
| `agent/desktop_local/source_resolver.py` | `Source.is_remote`（桌面解析器恒为 `False`），让调用方统一提问 |
| `channel/web/fork/handlers/workspace.py` | `_coding_remote`/`_coding_remote_service` 绑定探测；`WorkspaceTree/Search/Resolve/Meta/Read` 增加远端分支；新增 `WorkspaceDownloadHandler` |
| `channel/web/route_registry.py`、`web_channel.py` | 注册 `GET /api/workspace/download` |
| `tests/test_coding_remote_files.py`、`tests/test_coding_remote_workspace.py`（新增） | 客户端/DTO 单测与面板路由测试 |

关键规则：

- **来源由会话链接决定**：链接 `workspace_id` 非空即为个人会话，`project_dir` 即远端 `U`；非空但 `service_id` 与当前配置不符时以 `409 coding_service_changed` 拒绝，绝不交给后端。
- **位置恒定**：每个文件请求都把 location 钉在已核验的 `U`，客户端不能传 directory。
- **引用是单个相对 id**：`normalize_relative` 先归一化并拒绝绝对路径、UNC、盘符（`:`）与 `..`，再由远端兜底；不是先 sanitize。
- **`500` 是"不存在"不是"服务坏了"**：远端对缺失/越界/类型不符一律 `500`，客户端映射为 `FileNotFoundError`（面板报 not-found），真正的宕机在传输层已是 `502/504`。
- **下载不走本地盘**：`/api/workspace/download` 仅对远端来源生效，非远端一律 `404`，不会成为读平台本地文件的第二条路径。
- **远端条目不带平台本地路径**：条目无 `abs_path`/`raw_url`/`preview_url`，文件仅给相对的 `download_url`；`read_text` 的 `editable` 恒为 `False`（本切片只读）。

### 9.2 真实联调（平台读客户端 -> 127.0.0.1:4096）

临时 `P/U`，用后删除：

| 步骤 | 真实结果 |
| --- | --- |
| `prepare_workspace(P,U,wid)` | `ready`（`U` 真实存在） |
| `CodingRemoteFiles.list_dir()` | `['uploads','notes.md']`；`uploads` 为 `is_dir=true, kind=directory, previewable=false` |
| `list_dir('uploads')` | `['uploads/hello.txt']` |
| `search('hello')` | `['uploads/hello.txt']` |
| `read_text('uploads/hello.txt')` | `'live hello'`，`editable=False` |
| `read_bytes('notes.md')` | `b'# live notes'`，`text/markdown` |
| `normalize_relative('../secret')` | `None`（逃逸引用被拒） |

### 9.3 验证命令与结果

```bash
python -m pytest tests/test_coding_remote_files.py -q                    # 34 passed
python -m pytest tests/test_coding_remote_workspace.py -q                # 15 passed
python -m pytest tests/test_coding_personal_workspace.py tests/test_opencode_client.py \
  tests/test_coding_session_routes.py tests/test_coding_agent_routes.py \
  tests/test_coding_session_store.py -q                                  # 141 passed, 13 subtests
python -m pytest tests/test_agent_user_file_http.py tests/test_workspace_upload_delete.py \
  tests/test_console_workspace_transport.py tests/test_shared_root_users_tree.py \
  tests/test_desktop_source_resolver.py tests/test_route_registry.py \
  tests/test_web_module_seams.py -q                                      # 全绿
```

新增测试覆盖：请求形状（deepObject location、整段路径编码、`500`→not-found、`400`→ValueError、`401`→排除在用户 4xx 之外）、相对 id 归一化与逃逸拒绝、远端条目的 `kind/previewable` 与 dirs-first 排序、隐藏文件过滤、search/stat/read/read_bytes、`editable=False`、面板路由（tree/search/resolve/read/meta/download）、非远端下载拒绝、逃逸下载拒绝、缺失文件 404、绑定探测（普通会话/无链接/服务不符 409/能力关闭）。

### 9.4 既有失败（与本次无关）

- `tests/test_compat_surface_closure.py` 2 例（退役端点调用、能力开关读者清单）：以 `git stash -u` 暂存本次全部改动后复现同样失败。
- `tests/test_private_agent_file_scope.py::...::test_file_serve_still_serves_shared_agent_files` 与 `tests/test_upstream_drift_guards.py::TasksPagePortDriftTests`：同样在暂存本次改动后复现，属既有环境/上游漂移问题。

### 9.5 仍未实施

- 远端**写入/删除/移动/回收站**（本切片只读，前端应按 capabilities 禁用）。
- 3.2 前端来源切换（`workspace.js` 的 scope epoch、按钮按 capabilities 禁用）、3.3 附件落盘、3.4 输出目录、3.5 上传配额/审计。
- 远端条目的 size/mtime：远端 `FileSystemEntity` 只有 `{path,type}`，本实现如实置 0，未伪造。

## 10 远端上传、capabilities、前端来源与 Git 排除（task 3.1 上传 / 3.2 / 4.1）

### 10.1 远端改动（RSMCode）

- 协议 `packages/protocol/src/groups/coding-workspace.ts` 新增 `codingWorkspace.upload`（`POST /api/coding/workspace/upload`，`{workspace_id, relative_path, content_base64}`）。**位置来自服务端归属记录，不取自请求**：请求只给绑定 id 与 `U` 内相对路径。
- 处理器 `packages/server/src/handlers/coding-workspace.ts::uploadWorkspace`：解码 base64、`safeRelative` 拒绝绝对路径/盘符/`..`、`fs.mkdir` 父目录、在 **realpath 之后**用 `within(realDir, realParent)` 做包含校验（符号链接不能改写目标）、同名自动 `name (n).ext`、临时文件 `wx` 写入后 `rename` 原子发布、`MAX_UPLOAD_BYTES`(256 MiB) 上限、未 prepare 的绑定 `409`。
- 同一处理器的 `prepareWorkspace` 新增 `ensureGitExclude`：`P` 是 git 工作树且 `git ls-files -- user` 有跟踪文件 → `409`（用途冲突）；否则把 `user/` 追加到仓库本地 `.git/info/exclude`（不写被跟踪的 `.gitignore`）；非 git 项目或 git 不可用一律不阻塞 prepare。

### 10.2 平台改动

- `agent/coding/opencode.py::upload_file`：把 part 字节 base64 后 POST 到 `/api/coding/workspace/upload`；`400`→`ValueError`（路径被拒），`409`→`CodingError(coding_workspace_conflict)`，其余→upstream unavailable。
- `agent/coding/files.py::CodingRemoteFiles.upload_file`：先 `normalize_relative` 拒逃逸、无 `workspace_id` 拒 `coding_workspace_conflict`，回包路径再次归一化。
- `agent/coding/files.py::RemoteSource.describe`：新增 `label`（账号目录名）与**真实 `capabilities`**（read/download/upload 为 true；write/rename/move/delete/trash 为 false）。
- `channel/web/fork/handlers/workspace.py`：`WorkspaceUploadHandler` 增 `coding_remote` 分支（`_upload_remote`，`_read_part` 复用 `WS_UPLOAD_MAX_BYTES` 上限与 `too_large` 码；无子目录按 `uploads/<session>` / `uploads/manual` 落点）。**写/删除/回收站对远端一律 `400 unsupported_source`**（`_remote_unsupported`），避免相对 id 落到平台自身文件 API。
- `channel/web/static/js/workspace.js`：`wsCurrentSource` 识别 `coding_remote`→`remote`；`wsCapabilities` 记录来源能力（未知来源保持既有全量行为）；`wsAllows()` 门控删除行控件、回收站按钮、编辑器；`wsCanWriteHere()` 对远端按 upload 能力放行；`wsRenderPreview`/下载按钮对远端走 `/api/workspace/read` 与 `download_url`；`wsIsRemoteFile`/`wsRemotePreviewText` 新增；root crumb 用远端 `label`。

### 10.3 真实联调（平台 `OpenCodeClient` -> 127.0.0.1:4096）

`POST /api/coding/workspace/prepare` 建 `U` 后：

```
prepare: ready
upload1: {"path":"uploads/manual/note.txt","name":"note.txt","size":17,"renamed":false}
upload2: {"path":"uploads/manual/note (1).txt","name":"note (1).txt","size":6,"renamed":true}
tree: ['uploads/manual/note (1).txt', 'uploads/manual/note.txt']
read back: live upload bytes
caps: {"read":true,"download":true,"upload":true,"write":false,"rename":false,"move":false,"delete":false,"trash":false}
escape refused: the path escapes the personal directory
no-wid refused: CodingError coding_workspace_conflict
```

`/doc` 已列出 `/api/coding/workspace/upload`。

### 10.4 验证命令与结果

- `tests/test_coding_remote_files.py` + `tests/test_coding_remote_workspace.py`：**69 passed**（含 4 例客户端 upload、5 例 DTO upload、capabilities、write/delete/trash 远端 `unsupported_source`）。
- `tests/test_console_workspace_frontend.cjs`：**26 passed**（含远端来源启用上传/隐藏编辑删除、后端来源保留删除、`wsIsRemoteFile`）。
- `node --test tests/test_console_workspace_delete_frontend.cjs tests/test_console_workspace_upload_frontend.cjs`：**62 passed**。
- `rsmCode` `tsgo -p packages/server`、`-p packages/protocol`：无错误；`httpapi-exercise --mode coverage --fail-on-missing --fail-on-skip`：**pass=220 fail=0 missing=0**（新增 upload 成功/重名/逃逸/未准备 与 prepare exclude/tracked 场景）；`--mode auth` **pass=220 fail=0**。
- 回归：`test_workspace_upload_delete.py` + `test_agent_user_file_http.py` **66 passed**；上一轮 `test_console_workspace_transport/shared_root_users_tree/...` 集合 **201 passed**。
- 既有失败：`tests/test_desktop_project_source.cjs` 在本机因 `spawnSync npx ENOENT`（环境缺 npx）失败，与本次改动无关（失败发生在 `node --test` 的 `npx tsc` 编译步骤）。

### 10.5 本节的遗留项（关闭情况见第 11 节）

- 3.3 附件（iframe/拖拽/粘贴/场景）先落盘后发送、草稿保留；3.4 默认输出位置；3.5 上传配额/审计接入与临时文件清理 —— **已在第 11 节实现**（限额/审计一节如实记录了“无适用硬配额、未新建账本”）。
- 4.2 `U` 读取 `P` 的配置发现实测与 SAP 导出落点；4.3 跨租户/越权/撤权真实链路 —— **已在第 11 节处理**（配置发现在真实远端核对；跨租户/撤权为接入点级测试，真实链路如实记为未执行）。
- 5.1–5.3 真实远端端到端上传→处理→生成→预览下载与开关/回退的生产验证 —— **已在第 11.6 节执行**（生产开关回退演练仍如实记为未执行）。
- **仍未实施**：远端 upload 走 JSON+base64（effect httpapi 的类型化 JSON 契约），未做真正的流式落盘；大文件内存占用为 1.33×，README/任务未据此扩大声明。远端条目的 size/mtime 如实置 0。

## 11 附件、输出、限额、配置发现与越权/撤权（task 3.3–3.5 / 4.2 / 4.3）

日期：2026-10-11。状态：**已实现**。第 10 节留下的五项（附件、输出位置、限额/回收、配置发现、越权与撤权测试）在本节关闭，并附 5.1–5.3 的真实远端端到端验收。

### 11.1 附件先落盘后发送（3.3）

| 位置 | 改动 | 验证 |
| --- | --- | --- |
| `channel/web/fork/runtime.py::_upload_coding_remote` | composer 的单文件与整目录上传在**发送前**写入 `U` 的 `uploads/<session>/<attachment_id>/`（无会话时 `uploads/manual`，整目录再带 `webdir_<upload_id>`）；回包带 `remote:true` 与**相对** `file_path`；图片 `preview_url` 指向 `/api/workspace/download`（钉住本人 `U`），从不下发平台本地路径 | `tests/test_coding_personal_attachments.py`（42 例） |
| `runtime.py::_personal_attachment_refs` | 发送前把每个附件按相对 id 归一化并在远端确认存在：逃逸 → `invalid_reference`、不存在 → `attachment_missing`，**整轮不派发**（控制台保留草稿，消息绝不带着缺失附件发出）；图片的 content part 由**已落盘的字节**构建，不读请求体 | 同上；`tests/test_coding_remote_workspace.py`（36 例）；`tests/test_console_workspace_frontend.cjs`（34 例） |
| `agent/attachments.py` | 附件新增 `data` 形态（已读出的字节）：`_encode_one`（文件）与 `_encode_blob`（字节）共用同一条编码管线，`attachments_from_context` 透传字节 | 同上 |
| `workspace.py::write_remote_attachment` | 稳定 `attachment_id` 让重试落在同名：相同字节复用、不同字节以 `attachment_conflict` 拒绝，已有附件不被静默覆盖 | `tests/test_coding_personal_attachments.py`；E2E 5.1 |

### 11.2 默认输出位置（3.4）

- 每轮在提示尾部追加 `[个人目录输出位置: outputs/<session>/<turn>]`。规则只有一处：`agent/coding/files.py::output_relative_dir/output_line`；`turn` 用本回合既有的 `request_id`（流与簿记已用的标识），不新增任务身份或索引。
- 位置是**声明**给会话的，平台不代写 `U`：真实远端没有 mkdir 路由，生成的文件由会话自身工具写在 `U`，随后走既有的列举/预览/下载。
- `console.js` 把它识别为平台注记行并从消息文本剔除（`PLATFORM_NOTE_LABELS`），历史回放不会把注记当成读者自己的话。
- `workspace.js::wsRefreshAfterRun` 在**运行结束**与**面板重新激活**两个时点重列；未开面板不动、正在编辑不动；迟到响应按既有 scope epoch 丢弃。没有新增索引、同步服务或事件订阅。
- 验证：`tests/test_console_workspace_output_marker.cjs`（6 例）、`tests/test_console_workspace_frontend.cjs`（34 例）、E2E 输出段（见 11.6）。

### 11.3 上传限制、原子发布与临时回收（3.5）

- **复用既有约束**：一请求一文件、`WS_UPLOAD_MAX_BYTES`(200 MiB) 上限、1 MiB 分块读取、`too_large`/`incomplete_upload` 等既有错误码、前端既有的进度与逐项重试全部沿用；身份与 CSRF 走既有 `require_management_write()`（`test_a_remote_upload_cannot_skip_the_csrf_gate` 钉住这条不能绕）。
- **远端**：`MAX_UPLOAD_BYTES`(256 MiB) 为服务端后盾；写入走 `wx` 临时名 + `rename` 原子发布（只有崩在 rename 之前才会留下临时件）；包含校验在 realpath 之后做，符号链接不能改写目标。
- **临时回收**：`reclaimTempFiles` 只在**本次要写入的那个目录**回收名字可识别的 `.coding-upload-*.tmp`，无账本、无清理调度、不扫全盘；用户自己的 `.tmp` 不受影响。
- **配额与审计（如实记录）**：平台硬配额口径是 `tool_calls`/`tokens`，不适用于文件上传；既有 workspace 上传路径本身不做按文件的额度记账或审计，因此**没有可绕过的既有接口**，本次也未新建账本、未声称有配额或审计覆盖。
- 真实联调：接近上限的 128 MiB 上传 2.75s、回读 0.48s，字节逐位一致（E2E 5.2）。

### 11.4 配置发现与 SAP（4.2）

- **真实远端（E2E 4.2 段）**：`GET /api/location?location[directory]=U` 报告项目根就是 `P`；从 `U` 看到的 skills、`GET /config`、`/experimental/tool/ids`、`/api/agent`（含 build 的权限规则）与从 `P` 看到的**逐字一致**，包括只写在 `P` 的 `p-marker` 技能。即“会话跑在 `U`、可信配置与工具来自 `P`”，未为个人目录复制安装。
- **SAP 绑定**：快照拆 `project`(P) 与 `runtime_dir`(会话运行目录)，`store.py::attach_coding_session` 按 `runtime_dir`（旧行回退 `project`）判归属；`http.py::_runtime_directory` 在开关启用时经**同一个** `CodingWorkspaceService` 解析 `U`，解析失败以 503 明确报错而不回落 `P`。
- **工具安装**：仍只在 `P` 一份，账号目录 `U` 不出现第二份 `.opencode`（`tests/test_sap_workbench_project_toolkit.py::test_personal_mode_keeps_one_install_under_the_project_root`）。
- **场景导出落点**：本场景后端**没有**已交付的文件导出/下载动作，因此没有可改动的落点；一旦交付，其落点由会话工作目录（`U`）决定，无需新增传送通道。未新增场景能力，也未把这项算作个人文件入口的验收。

### 11.5 越权、跨租户、撤权与并发（4.3）

| 关注点 | 针对性测试 |
| --- | --- |
| 跨租户不共享目录 | `test_coding_personal_workspace.py::CrossTenantIsolation::test_another_tenant_cannot_reuse_the_first_tenants_directory` |
| 越权引用他人会话 | `test_coding_remote_workspace.py::test_another_members_personal_session_is_refused_before_the_remote`（在到达远端之前就拒绝） |
| 服务实例不符 | `::test_a_link_from_another_service_is_refused_not_served` |
| 逃逸引用 | `normalize_relative` 在平台侧先拒（绝对路径/UNC/盘符/`..`），远端 `safeRelative`+realpath 兜底；E2E 逐项复验 |
| 撤权后文件不可达 | `::test_revoking_the_binding_withdraws_the_personal_directory` |
| 同账号多会话 / 两账号 | `test_two_sessions_of_one_account_share_one_directory_and_binding`、`test_two_accounts_never_share_a_session_or_a_directory` |
| 失败可重试 | `test_a_preparation_failure_is_retryable_and_does_not_fall_back`；E2E 的 prepare 幂等与删除会话后重 prepare |

**如实记录**：本机只有一个真实服务实例、一个租户，跨租户与撤权的**真实**链路无法在此构造；上表是接入点级别的针对性测试，加上真实远端的单租户联调。未以 mock 结果冒充跨租户上线证据。

### 11.6 真实远端端到端验收（5.1–5.3）

驱动方式：平台自身的 `OpenCodeClient`/`CodingRemoteFiles` 与 `write_remote_attachment` 直连 `127.0.0.1:4096`；脚本随证据保留为 `evidence/verify_e2e.py`（需要运行中的服务与 `RSM_OPENCODE_PASSWORD`，只用临时 `P/U` 并在结束时删除）。`P` 含 `opencode.json` 与 `p-marker` 技能、`U=P/user/LiveE2E`，URL 形如 `/api/workspace/*` 的 handler 由第 9 节的单测覆盖，本处为真实链路。

| 任务 | 真实结果 |
| --- | --- |
| 5.1 | prepare（`created:true`，重试 `created:false`）→ `uploads/<session>/<attachment_id>/quote.txt` 落盘 → 同 id 重试不产生第二份、不同字节被拒 → 面板列举（根只见 `uploads`，目录在前，无 `abs_path`/`raw_url`）→ 嵌套列举 → `search` 首次即命中 → `read` 文本一致且 `editable:false` → `resolve` 给出相对 id → `download` 字节与 sha256 一致；四个逃逸引用逐个被拒 |
| 5.1 生成 | 回合注记 `[个人目录输出位置: outputs/<session>/<turn>]` 与落点 `outputs/<session>/<turn>/report.md` 一致；面板列举到该回合目录；生成字节回读一致 |
| 5.2 | 128 MiB 载荷上传（报告 size=134217728）2.75s、回读 0.48s，字节与 sha256 完全一致，宿主文件确为 128 MiB |
| 5.3 | 在 `U` 建真实会话 → 写入一文件 → 删除会话（上游 404）后 `U` 仍存在、该文件仍在、重 prepare 仍解析到同一 `U` 且文件保留 |
| 4.2 | location/skills/config/tools/agents 从 `U` 与从 `P` 逐字一致（见 11.4） |

**观察（既有行为，非本次回归）**：`DELETE /session/:id` 在项目内含 `opencode.json`/`.opencode` 时约 28–33s（本机同时跑回归测试时观测到 42s）。把会话**直接放在 `P`**（完全不使用个人目录）同样耗时 28.8–32.3s，且个人目录的读写片段不改变耗时（no ops 与 full ops 相同）。这是 RSMCode 自身会话删除路径在带配置/技能项目上的行为，本 change 未触碰；E2E 因此只对该调用单独放宽读超时并保留重试，未修改任何产品代码去掩盖它。

### 11.7 验证命令与结果（2026-10-11）

```bash
# 平台：本次新接口与适配
python -m pytest tests/test_coding_personal_attachments.py tests/test_coding_personal_workspace.py \
  tests/test_coding_remote_files.py tests/test_coding_remote_workspace.py tests/test_opencode_client.py \
  tests/test_coding_settings.py tests/test_coding_session_store.py tests/test_sap_workbench_runtime.py \
  tests/test_sap_workbench_project_toolkit.py -q -p no:randomly
# 268 passed, 13 subtests passed

# 平台：回归集合（会话/路由/工作区/桌面来源/注册表/前端接缝 + 本次三个远端套件）
python -m pytest tests/test_coding_session_routes.py tests/test_coding_agent_routes.py \
  tests/test_coding_agent_type_boundary.py tests/test_coding_session_lifecycle.py \
  tests/test_coding_session_sync.py tests/test_coding_browser_auth.py \
  tests/test_coding_model_preferences.py tests/test_conversation_schema_seam.py \
  tests/test_workspace_upload_delete.py tests/test_agent_user_file_http.py \
  tests/test_console_workspace_transport.py tests/test_shared_root_users_tree.py \
  tests/test_desktop_source_resolver.py tests/test_route_registry.py tests/test_web_module_seams.py \
  tests/test_coding_remote_files.py tests/test_coding_remote_workspace.py \
  tests/test_coding_personal_attachments.py -q -p no:randomly
# 447 passed（0 failed，12:11）

# 前端（node）
node --test tests/test_console_workspace_frontend.cjs        # tests 34, pass 34, fail 0
node --test tests/test_console_workspace_output_marker.cjs   # tests 6, pass 6, fail 0
node --test tests/test_console_workspace_delete_frontend.cjs tests/test_console_workspace_upload_frontend.cjs
                                                             # tests 62, pass 62, fail 0

# 远端（RSMCode）
bun x tsgo --noEmit -p packages/server/tsconfig.json     # exit 0
bun x tsgo --noEmit -p packages/protocol/tsconfig.json   # exit 0
bun x tsgo --noEmit -p packages/core/tsconfig.json       # exit 0
bun test test/filesystem/search-find.test.ts test/filesystem/search.test.ts   # 5 pass, 0 fail
bun run script/httpapi-exercise.ts --mode coverage --fail-on-missing --fail-on-skip
# summary pass=221 fail=0 skip=0 missing=0 extra=0
bun run script/httpapi-exercise.ts --mode auth --fail-on-missing --fail-on-skip
# summary pass=221 fail=0 skip=0 missing=0 extra=0

# 真实远端端到端
python openspec/changes/add-coding-agent-personal-workspace/evidence/verify_e2e.py
# all checks passed（5.1/5.2/5.3 与 4.2 段全 PASS；5.2 为 128 MiB）

# OpenSpec 严格校验
bun x --bun @fission-ai/openspec validate add-coding-agent-personal-workspace --strict
# Change 'add-coding-agent-personal-workspace' is valid
```

### 11.8 仍未实施（如实记录）

- 远端 **写入/删除/移动/回收站**按设计为可选且无兼容实现：`RemoteSource.capabilities` 如实置 false，前端隐藏，写/删除/回收站 handler 对远端一律 `400 unsupported_source`。
- 远端 upload 走 JSON+base64（effect httpapi 的类型化 JSON 契约），未做真正流式落盘；大文件内存占用约 1.33×，未据此扩大任何声明。
- 远端条目的 size/mtime 如实置 0（远端 `FileSystemEntity` 只有 `{path,type}`，未伪造）。
- 旧会话面板“仍在原项目执行”的提示 UI 由第 6 节的 `ws_legacy_session_notice` 承载，未做额外引导。
- 跨租户/撤权的真实（非接入点级）链路、生产开关回退演练：本环境无法构造，未执行。

