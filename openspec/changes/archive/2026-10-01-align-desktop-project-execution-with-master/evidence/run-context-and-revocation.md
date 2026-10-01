# 运行中的目录切换、清除与撤权取消（任务 3.5）

对应主规范 `desktop-project-execution` 的「每轮使用已验证的执行目标」与
「无项目与旧只读模式保持兼容」。本文记录**实际改了什么、怎么验证的、哪里没做**。

## 一、交付内容

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| 本轮固定执行目录 | `common/runtime_identity.py`（`execution_cwd`）、`channel/web/fork/execution_scope.py`（入口解析一次并冻结） | 目录在**消息入口**解析成值，之后不再询问 |
| 运行入口真正带上目标 | `channel/chat_channel.py::_handle` | 工作线程拿到的只是请求身份的**快照**（不含目标），原先这条路径上 `current_identity()` 没有 `execution_target`，工具层根本读不到；现在在唯一入口 `_handle` 用 `execution_target_scope(agent_id, session_id)` 发布，本地/远端/IM 通道共用同一处 |
| 每轮独占工具视图 | `agent/desktop_local/run_context.py::tool_view_for_run`、`agent/protocol/agent_stream.py::_run_tool` | `read/write/edit/bash/ls/search_files` 等按轮浅拷贝并钉住本轮目录；`Agent.apply_project_dir()` 改的是**共享**工具，因此并发轮次无法移动在跑的轮次 |
| 失效即拒绝、不回退 | `run_context.py::run_local_cwd`、`agent_stream.py::_execute_tool` 的本地门禁 | 目标在、目录不在（或授权查不到、版本变了、目录被删）→ 明确拒绝，**不**改用服务器目录；只拒绝**需要该目录**的调用（`run_context.py::needs_local_directory`），记忆/知识等服务器侧工具照常 |
| 结果归属本轮目录 | `agent_stream.py::_maybe_emit_artifact` | 工件锚点改用**本轮**目录（`_run_cwd`），否则本机写出的文件会被当成“在锚点之外”丢掉 |
| 每轮记录本机作用域 | `run_context.py::local_run_scope`、`bridge/agent_bridge.py::chat` | 取消令牌带上本轮目标的**标识符**（用户/租户/设备/工作区/绑定/授权版本/用途），**不含路径** |
| 按作用域取消 | `agent/protocol/cancel.py::cancel_scope` | 只有“带作用域且每条给定条件都匹配”的运行才取消；无本机项目、别的用户、别的设备的运行一律不动 |
| 撤权一处生效 | `run_context.py::revoke_local_scope`：注销本机根 + 取消同作用域运行 | 两半必须同时发生：只注销根会让模型继续在已关闭的目录里空转，只取消运行会让后续轮次又能走进去 |
| 撤权接入点 | `integrations/desktop/local_root.py::revoke_root`（原生关闭项目/切账号/切服务器）；`integrations/desktop/devices.py` 的 `disable_device` / `revoke_binding` / `revoke_workspace` / `revoke_for_user`（登出）/ `revoke_for_membership`（成员撤权，带租户） | 同机进程内的本机授权与控制台侧授权记录一起失效，且都只限定在调用者自己的用户范围内 |
| 清除本机项目恢复默认 | `bridge/agent_bridge.py::_apply_session_project` | 会话不再指向本机目标时清掉 Agent 上的旧目标（`Agent.clear_execution_target()`），再走原有的服务器项目/个人默认目录规则空转；只读绑定不被自动升级 |

“运行中切目录只影响下一轮”之所以成立，是因为：每轮一条消息入口
（`execution_target_scope`）解析一次目录；每轮一个 `AgentStreamExecutor`
（`agent/chat/service.py` 与 `Agent.run_stream` 都是按轮构造），而工具视图是
按执行器构建的，所以“这一轮”就是一个可界定的对象。

## 二、验证

命令固定为 `.venv/bin/python -m pytest <子集> -q -p no:randomly`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_run_context.py` | **33 项通过** | `run_local_cwd`（无目标不是拒绝 / 冻结缺失即拒绝 / 撤权 / 重选版本递增 / 目录被删 / 目录被移动 / 记录损坏）、`tool_view_for_run`（钉住本轮、不动 Agent 原对象、事后改指向不移动已构建视图、config 逐轮拷贝、无目录工具共享、无法改指向的工具被丢弃）、执行器门禁（无目标走原路径、本机轮次落在冻结目录、拒绝时**工具未执行**、拒绝在事件上可见、拒绝不计入连续失败、**在途切换不移动本轮**、撤权/重选下一调用即拒绝、**下一轮用新目录**、服务器侧工具不受项目关闭影响、未知工具不背锅、解析器异常保持原行为） |
| `tests/test_desktop_run_scope_cancel.py` | **24 项通过** | `local_run_scope`（无目标为空 / 标识符齐全 / 不含目录）、`cancel_scope`（匹配才取消、跨作用域不动、无作用域不动、跨用户不动、任一条件不符不动、**空条件一条也不取消**）、`revoke_local_scope`（根与运行同时消失、窄化撤权、跨用户不动、整用户撤权清空多设备）、`revoke_root` 端点（关项目即停运行、只停该工作区/该设备、**别人的撤权够不到**）、`devices._invalidate_local` 的三条范围规则、`_handle` 入口（本机轮次看得到目标与冻结目录、无项目会话看不到、**解析失败时只有目标没有目录**） |
| `tests/test_desktop_execution_target_wire.py` | **24 项通过** | 原有接缝用例 + 新增「清除本机项目后恢复默认」 |
| `tests/test_desktop_local_root.py` | **53 项通过** | 注册/解析分支回归（`revoke` 新增 `binding_id` 收窄未破坏原有断言） |
| `tests/test_desktop_local_worker.py` | **44 项通过** | headless worker 回归 |

一次合跑：`tests/test_desktop_run_context.py tests/test_desktop_execution_target_wire.py
tests/test_desktop_run_scope_cancel.py tests/test_desktop_local_root.py
tests/test_desktop_local_worker.py` → **178 项通过**。

受影响通道子集：`tests/test_chat_identity_context.py tests/test_channel_type_admissibility.py
tests/test_external_im_gate.py tests/test_personal_channel_inbound.py
tests/test_tenant_channel_inbound_anchor.py tests/test_tenant_channel_inbound_closure.py
tests/test_tenant_channel_inbound_isolation.py tests/test_wecom_bot_inbound_identity.py
tests/test_identity_boundary_contract.py tests/test_agent_steering.py`
→ **151 项通过、1 项既有失败**（见第四节）。

Agent/共享资源子集：`tests/test_agent_admin.py tests/test_agent_registry.py
tests/test_agent_onboarding_copy_wire.py tests/test_agent_workbench.py
tests/test_shared_asset_prompt_guidance.py tests/test_skill_public_surface_scope.py
tests/test_agent_roster_write_isolation.py` → **132 项通过、1 项既有失败**（同上）。

### 变异检查（证明用例真的能失败，而不是碰巧通过）

1. 把 `_execute_tool` 的本地门禁整段换成 `self.tools.get(tool_name), None`
   → `tests/test_desktop_run_context.py` **8 项失败**（本机轮次落在冻结目录、在途切换、
   下一轮用新目录、撤权/重选下一调用、拒绝不入失败计数、拒绝可见、不可解析即拒绝）。
2. 把拒绝范围放宽为“一律拒绝”（去掉 `needs_local_directory`）
   → `test_a_server_side_tool_still_runs_when_the_project_is_gone` 失败。
3. 把 `_handle` 的 `execution_target_scope` 换成空操作（运行时替换，不改源文件）
   → `RunTargetScopeTests` 3 项中 2 项失败。

## 三、明确边界（不勾选的部分）

- **Windows 与安装包内**：本轮只做 macOS 开发机上的真实断言，第 4/10 组的平台门槛
  未完成，因此桌面脚本能力状态不受本文影响。
- **资源上限**：本文不涉及内存/CPU 硬上限（见 `evidence/isolation-acceptance.md` §5）。
- **远端执行通道**：本机项目在**远程**模式下经由设备命令执行（第 6 组）尚未接通，
  因此“撤权取消”对本机根与 Agent 运行两条链都在同机进程内验证；远端链路的
  双端重验与取消属第 6/7 组。

## 四、本批次观察到的既有失败（非本次改动引入）

在该变更**之前**的 commit 上以相同环境跑同一批用例，同样失败，因此不顺手修改，
单独记录：

1. `tests/test_shared_asset_prompt_guidance.py::test_guidance_reaches_the_llm_request`
   —— 该用例用 `Agent.__new__(Agent)` 手工拼装实例，未设置 `workspace_scope`；
   `get_full_system_prompt()` 读到缺属性后回退缓存提示词，断言随之失败。
   在 `git worktree` 检出的 HEAD（`7d4cf3db`）上复现同样的失败。
2. `tests/test_wecom_bot_inbound_identity.py::test_an_unbound_sender_is_unbound_and_not_unsupported`
   —— 依赖本机真实部署身份数据（`_preflight_external_inbound` 把未绑定发送者映射成了
   真实用户），属已知环境依赖型失败；把 `channel/chat_channel.py` 单独还原到 HEAD
   后同样失败。
