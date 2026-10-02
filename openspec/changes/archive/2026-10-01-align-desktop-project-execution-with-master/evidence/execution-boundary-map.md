# 执行边界与接入点地图（实施输入）

本文记录**已核对过的现有代码事实**，作为第 3—5 组实施的接入点依据。核对方式为直接
读代码 + grep，不是推断。本文不声明任何能力已验收。

## 1. 桌面侧命令面（执行 v2 必须扩展的部分）

`desktop/src/main/remote/device-ops.ts`：

- `SUPPORTED_OPS = ['list', 'stat', 'search', 'read_text']`；`runDeviceCommand` 为穷举
  `switch`，`default → feature_unavailable`。`materialize` / `inspect`（`contracts/desktop/v1.json`
  的 ops）在当前实现中同样答 `feature_unavailable`。
- 因此“本机执行”不是给现有读取通道加开关，而是新增 op、新增工具分发与新的终态语义。

`desktop/native/fs-guard/src/main.rs`：`dispatch` 的 `match` 只有
`open_root / close_root / list / stat / read / search / cancel`，`_ => Err(Code::UnknownOp)`
（main.rs:262）。同文件用例显式断言 `exec` 与 `write/unlink/rename/shell/chmod/spawn`
均为 `UnknownOp`（main.rs:360—365）。**该 helper 在构造上只读**，与 `design.md` D5
“需要新的执行启动器而不是放宽 helper”一致。

结论：第 4 组的平台启动器是**新组件**，不能复用 fs-guard 的协议与授权模型；fs-guard 继续
只承担只读文件访问。

## 2. 数据库模式下实际生效的调用门禁

`agent/protocol/agent_stream.py::_permission_denial`（2264—2333）在**每次调用**按序执行：

1. `isolation_decision(tool_name, arguments, cwd=agent.effective_cwd())` → `"isolation"`
2. `_resource_tool_denial`（`tool.execute` + 资源授权）→ `"role"`
3. `_quota_tool_denial` → `"quota"`
4. `_approval_tool_denial` → `"approval"`
5. 异常 → fail-closed `"identity"`

**`check_tool_call`（`agent/permission/policy.py:402`）不在这条路径上**：全仓仅有的引用是
其定义与 `agent/permission/__init__.py` 的导出。这不是遗漏——归档 change
`2026-09-10-rbac-controlled-execution-permission/tasks.md:5` 明确记录“仅在非 database 模式
执行 `check_tool_call`；database 模式保留隔离、`tool.execute` 资源授权与配额”。因此
`agent_stream.py:2107` 的 `denial_kind == "mode"` 分支在 database 模式下**永远不会命中**，
是那条归档决策的结果。

对本 change 的影响（第 3 组必须先决定）：`execution-isolation` 的 MODIFIED requirement 要求
“现有会话权限模式（read-only / workspace-write / full-access）按执行目标的获权隔离根收紧
解释”，但今天 database 模式的分发路径上**没有任何东西读会话权限模式**。本机项目执行必须
显式把权限模式接入本机边界（或明确说明本机以 grant 用途 + 隔离边界为准），不能假定
`check_tool_call` 会兜住。

## 3. 每轮上下文的现有容器

`common/runtime_identity.py`：`RuntimeIdentity`（agent_id / user_id / tenant_id /
session_id / run_id / web_auth_session_id）由 ContextVar 承载，`identity_scope(**overrides)`
供子代理派生，`derive(...)` 生成新实例。**没有执行目标（execution_target）字段**，
`cwd` 也不在其中。

`bridge/agent_bridge.py::_attach_desktop_context_to_tools`（618—639）是既有的“把服务端已核验的
逐轮绑定挂到工具实例”先例，只服务 `client_files`；第 3 组的通用执行上下文可沿用同一形状。

## 4. cwd 的现状（不可变运行上下文要替换的对象）

- cwd 在**装配期**写入工具实例：`bridge/agent_initializer.py::_load_tools`（539—667，
  `file_config = {"cwd": workspace_root, ...}`，随后 `tool.cwd = ...`）。
- 每轮取 Agent 时按会话**原地改写**：`bridge/agent_bridge.py::_apply_session_project`
  （1203—1235）→ `Agent.apply_project_dir`（`agent/protocol/agent.py:212—255`），后者遍历
  `_CWD_TOOLS`（203—206）逐个 `set_cwd` / `tool.cwd = cwd`，**不调用 `os.chdir()`**。
- `AgentStreamExecutor.__init__` 把工具收敛为 `self.tools = {tool.name: tool}`（agent_stream.py:293），
  并在 `_run_parallel_calls`（2007—2045）用 `copy.copy(self.tools[...])` 隔离并行调用。
- 每轮唯一的文件系统输入是 `agent.effective_cwd()`（`_permission_denial` 2285、
  `_maybe_emit_artifact` 538）。

接入点结论：不可变运行目标应在 `AgentStreamExecutor` 构造/`_execute_tool` 的逐调用注入块
（2184—2197）落地，而不是依赖 `apply_project_dir` 曾在取 Agent 时运行过；共享工具实例不得在
运行中被改写 cwd。

## 5. 服务端侧

- 桌面链路 `desktop_devices → desktop_bindings → desktop_workspaces → desktop_binding_workspaces`
  全部**没有绝对路径列**（`auth/store.py:2275—2354`），`DesktopWorkspacesHandler`
  显式拒绝 body 里的 `absolute_path`/`path`/`root`（`channel/web/fork/handlers/desktop.py:300—312`）。
- 现有权限判定集中在 `integrations/desktop/access.py`（`load_binding` / `verify_binding_scope` /
  `prepare_binding_create`），逐轮引用由 `integrations/desktop/session_context.py` 解析并核验。
- 个人根限制有三处：`agent/workspace/project_browser.py::trusted_root`、
  `agent/workspace/project_store.py::_require_within_user_root`、
  `channel/web/fork/runtime.py::_is_path_allowed`。D4 要求新增本机来源 resolver，而不是放宽这三处。
- 全仓当前**没有** `execution_target` / `project_mode` / `location` 字段（grep 无命中），
  与 `proposal.md` “旧数据按 backend/readonly-input 解析”的迁移前提一致。
