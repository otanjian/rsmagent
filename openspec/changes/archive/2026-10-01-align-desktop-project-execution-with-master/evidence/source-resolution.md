# 一处解析：文件面板、`@` 引用与工具参数（任务 3.6）

对应主规范 `desktop-project-execution` 的「既有交互面无条件上传与服务器普通
路径解析」与 `desktop-project-artifacts` 的「文件面板显示本轮项目内容」。本文记录
**实际改了什么、怎么验证的、哪里没做**。

## 一、交付内容

要求是「文件面板、附件工作区引用及 `@` SHALL 使用同一来源；本机引用不经过服务器
普通路径解析或无条件上传」。实现方式是**一个解析器 + 三个调用面**，而不是三处各自
判断。

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| 唯一来源解析器 | `agent/desktop_local/source_resolver.py` | `source_for_session()` / `source_for_identity()` 拿会话**存下来的目标**（只有标识符）后，用 `run_context.resolve_target_root()` **当下重验**；解析不出来就返回 `Source(kind='desktop', root=None, refusal=…)`，**绝不**给出服务器根 |
| 一个目标一个答案 | 同上 | 面板列出的目录与工具读到的目录走同一函数，因此「面板列了 X」与「工具读了 X」不可能不一致；无本机目标的会话保持原有服务器行为**一字不变** |
| 本机引用不是服务器路径 | 同上 `resolve_reference()` | 本机来源下绝对路径**不解析**直接拒绝（`REFUSAL_SERVER_PATH`）；只接受项目内向下相对 id，且按 **realpath** 复核包含关系，符号链接换掉某一层也逃不出去 |
| 落地是显式能力 | 同上 `prepare_tool_inputs()` | 本机运行拿不到的服务器输入（上传件、抓取件）**按名拒绝**（`REFUSAL_NO_LANDING`），并说明可以走获权传输落地；注入 `transfer` 时落地结果也必须落在项目内，否则拒绝。本模块**从不上传** |
| 文件面板 | `channel/web/fork/handlers/workspace.py::_panel_source/_panel_service/_panel_relative/_panel_reference/_panel_entry` | 树、搜索、解析、元信息、读、写六个入口都先取来源：本机项目不可用 → `503 local_project_unavailable`（不是列服务器目录）；本机条目标 `source: "desktop"`+`local: true` 并**不给** `raw_url`/`preview_url`/`abs_path`；写盘的相对路径经 `_panel_relative` 走同一套包含检查；记忆/知识资产仍留在服务器侧 |
| `@` 引用 | `channel/web/fork/runtime.py::post_message` 的 `workspace_ref` 分支 | 附件不再假定服务器路径，而是 `source_for_session()` + `reference_line()`：本机引用渲染成 `[本机项目文件: …]` / `[本机项目目录: …]`，被拒绝的渲染成 `[本机引用未生效: …]`（**不静默丢弃**），全程不查服务器 stat、不上传 |
| 工具参数准备 | `agent/protocol/agent_stream.py::_stage_local_inputs` | 本机轮次的 `path`/`paths`/`file_path`/`directory`/`dir` 在执行前过同一解析器：项目内绝对路径改写成相对 id，项目外/服务器路径拒绝并回 `local_input_unavailable: true` 事件；被拒绝的调用**整条不执行**（参数悄悄丢掉会看起来像成功） |

拒绝语义统一由 `Source.refusal` 一个字段承载，`Source.describe()` 是唯一可以进响应
的形状（只含 kind/available/refusal，**不含目录**）。

## 二、验证

命令固定为 `.venv/bin/python -m pytest <子集> -q -p no:randomly`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_source_resolver.py`（新增） | **55 项通过** | `RelativeIdTests`（5）相对 id 语法：空/None 即根、`..`/`//`/`.` 分量拒绝、绝对与 Windows 盘符拒绝、反斜杠在任何平台都拒绝；`SourceTests`（11）来源派生：无目标即服务器、本机来源当下重验、撤权即拒绝**而非服务器根**、重选版本递增后旧目标拒绝、目录被删、`describe()` 不含路径；`ReferenceTests`（11）引用解析：绝对路径**不被解析**直接拒绝、逃出项目拒绝、缺失按名报告、服务器相对 id 只归一化、服务器绝对路径原样交还；`ReferenceLineTests`（5）提示行：本机文件/目录标签、拒绝可见且写明未上传；`ToolInputTests`（13）参数准备：项目内绝对路径改写、列表逐项检查、项目外拒绝、服务器输入按名拒绝且未上传、落地传输在项目内接受/项目外拒绝/抛异常算拒绝、参数为副本；`ExecutorLocalInputTests`（5）执行器门禁：拒绝发生在工具执行前、事件带 `local_input_unavailable`、服务器侧工具参数不受影响、无本机项目时原样放过、撤权后拒绝；`PanelSourceTests`（5）面板：本机会话由本机目录服务、撤权会话 503 而非列服务器、无项目会话行为不变、本机条目无服务器 URL、服务器条目照旧 |
| `tests/test_desktop_run_context.py tests/test_desktop_run_scope_cancel.py tests/test_desktop_execution_target_wire.py tests/test_desktop_local_root.py tests/test_desktop_local_worker.py tests/test_workspace_edit.py tests/test_workspace_user_dir.py tests/test_project_db_containment.py tests/test_admin_audit_console_http.py` | **299 项通过** | 3.5 已有的目录冻结/撤权取消/工件锚点回归，加上面板与写盘相关套件：本任务改的 `runtime.py`、`workspace.py` 共用路径没有破坏既有行为 |
| `tests/test_desktop_transfer.py tests/test_fork_multipart_agent_scope.py tests/test_history_agent_workspace.py tests/test_desktop_web_session.py tests/test_desktop_auth_flow.py` | **88 项通过** | fork 侧消息组装与传输：`workspace_ref` 改动后附件、历史与 web 会话路径照旧 |

### 变异检查（证明用例真的能失败，而不是碰巧通过）

脚本 `evidence/scripts/mutate_source_resolution.py`（可重跑）逐条把实现改成一条
「看起来更省事」的写法，跑同一批用例（`tests/test_desktop_source_resolver.py` +
`tests/test_desktop_run_context.py`），出现预期失败后立刻还原源文件并复查内容一致。
输出见 `evidence/source-resolution-mutations.log`，退出码 0。

| 变异 | 汇总 | 抓到的用例 |
|---|---|---|
| M1 本机项目解析不出来时**回退服务器目录** | 2 failed, 86 passed | `SourceTests::test_a_session_whose_grant_was_revoked_is_refused`、`PanelSourceTests::test_a_revoked_local_session_refuses_instead_of_listing_the_server` |
| M2 绝对路径**当成项目内相对路径**解析 | 2 failed, 86 passed | `ReferenceTests::test_an_absolute_path_is_refused_and_never_parsed`、`ReferenceLineTests::test_a_refusal_is_visible_and_says_nothing_was_uploaded` |
| M3 服务器输入**不清示直接落地**成项目内文件（等于无条件上传） | 5 failed, 83 passed | `ToolInputTests::test_a_server_path_is_refused_by_name_and_nothing_is_uploaded`、`ExecutorLocalInputTests::test_a_server_path_refuses_the_call_before_the_tool_runs`、落地传输三条（项目外/抛异常/列表逐项） |
| M4 重验时**忽略授权版本**（旧目标继续可用） | 3 failed, 85 passed | `SourceTests::test_a_re_picked_directory_is_refused_for_the_old_target`、`RunLocalCwdTests::test_a_repick_invalidates_the_run`、`ExecutorLocalGateTests::test_a_re_picked_directory_stops_the_very_next_call` |
| M5 运行中重选目录后**静默跟随新目录** | 1 failed, 87 passed | `RunLocalCwdTests::test_a_moved_directory_does_not_silently_follow` |

## 三、明确边界（不勾选的部分）

- **远端执行通道**：本机引用在**远程**模式下经设备命令执行（第 6 组）尚未接通，
  因此「工具参数预落地」的传输回调由调用方注入，本轮只在同机进程内验证；
  服务器输入的真实落地传输属第 6/7 组。
- **平台与安装包**：只做 macOS 开发机断言，第 4/10 组未完成，桌面脚本能力状态不受本文影响。
- **面板前端**：本轮只保证服务端不再把本机项目按服务器路径解析、不再返回服务器
  URL；前端对 `source: "desktop"` 的呈现（本机徽标、打开/预览）属第 9 组。

## 四、本批次观察到的既有失败（非本次改动引入）

无新增。相关套件本次全绿；第 3.5 节记录的既有失败（`test_shared_asset_prompt_guidance.py`、
`test_wecom_bot_inbound_identity.py`）不在本任务子集内，未受影响。
