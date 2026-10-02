# A03 / A08 / A10 / A30 / A31 的目标解析与授权分支（任务 3.8）

对应 `acceptance.md` 的 A03 / A08 / A10 / A30 / A31 五行，验证的是这五行里
**目标解析**与**授权分支**的那部分（不是五行全部语义）。本文记录本任务新增的
A 编号级用例、每一行还剩哪些分支由既有套件承担、以及变异检查结果。

## 一、新增套件

`tests/test_desktop_target_resolution_acceptance.py`（19 项 + 16 子测试）：

| 行 | 类 | 断言要点 |
|---|---|---|
| A03 | `A03ForgedReferenceTests`(6) | 页面提交本机绝对路径在**本机来源**下被拒且不被当作服务器路径解析；`~` 与越界相对 id 各自按因拒绝并说明**没有改用服务器目录**；另一个用户的登记记录用同一组标识符**取不到**；目录被换（版本不变）后旧 handle 不再解析；重选版本递增后旧 handle 不再解析；服务器侧个人项目根校验（`_require_within_user_root`）未被本机项目放宽 |
| A08 | `A08CachedAgentSwapTests`(2) | 一个共享 Agent、两个会话各带自己的目标：并发轮次拿到各自的冻结目录与**各自的作用域标识符**，后一轮 `apply_project_dir` 不动前一轮；会话重指向只影响**下一轮**（在跑轮次仍读旧值） |
| A10 | `A10TeamHandoffTests`(3) | 接续给无项目工具资格的 Agent → 拒绝原因指向该 Agent（不是触发性质），而同一轮由主持 Agent 发出则通过（证明授权是活的）；收窄后需要目录的调用拒绝、`memory_search` 类工具不受牵连；`client_files`（能读本机的代理工具）走**同一套** `effective_capabilities` 门禁，不是侧门；有项目工具的 Agent 行为完全不变 |
| A30 | `A30NoProjectCompatibilityTests`(4) | 无本机目标**不是拒绝**（`run_local_cwd` 与授权判定都放行，来源为 server）；进入本机作用域**不改变**工具策略（共享资产限制不丢）；只读 grant 不能被读成执行；关闭项目后作用域退化为 no-op（而不是拒绝），会话回到既有服务器规则 |
| A31 | `A31NoBackgroundGrantTests`(4) | 同一轮"用户发出"放行、"调度/后台/机器主体"一律拒绝（拒绝来自触发性质，不是 grant 缺失）；后台轮次在 run context 里同样被拒且可在轮次结束后还原；`contracts/desktop/v1.json` 的 op 是**闭集**且不含 exec/shell/module 语义，伪造 op 与多余参数都被 `validate_command_frame` 拒绝；`desktop/src/main/preload.ts` 暴露给渲染进程的接口里**没有**任何命令通道 |

## 二、由既有套件承担的分支（不重复断言）

| 行 | 分支 | 既有用例 |
|---|---|---|
| A03 | 非 loopback / 带转发头 / 缺或错启动令牌被拒；Web 子会话 bearer 不能登记；相对路径被拒；别人的工作区不可见 | `tests/test_desktop_local_root.py::TransportGuardTests`、`::RegisterRootTests` |
| A03 | 客户端绝对路径在 `client_files` 契约层被拒、模型自填 binding 不能换目标 | `tests/test_desktop_local_context.py::ClientFilesToolTests` |
| A08 | 工具视图按轮浅拷贝、在途改指向不动本轮、下一轮用新目录 | `tests/test_desktop_run_context.py::ToolViewTests`、`tests/test_desktop_run_scope_cancel.py` |
| A10 | 资格判定的全部分支（allowlist/denylist/停用/未注册/技能未选中）与回复路径的"先摘缓存实例再拒绝" | `tests/test_desktop_run_authorization.py::AgentLocalEligibilityTests`、`::AgentReplyGateTests` |
| A30 | 会话无项目时 `@`/面板/工具参数保持既有服务器行为 | `tests/test_desktop_source_resolver.py` |
| A31 | 调度触发的上下文确实带 `task_source='scheduler'`；回复路径在触碰 Agent 之前拒绝 | `tests/test_desktop_run_authorization.py::SchedulerSourceTests`、`::AgentReplyGateTests` |

## 三、验证

命令固定为 `.venv/bin/python -m pytest <子集> -q -p no:randomly`。

| 项 | 结果 |
|---|---|
| `tests/test_desktop_target_resolution_acceptance.py` | **19 项 + 16 子测试通过** |
| 桌面子集（本文件 + `test_desktop_run_authorization` + `test_desktop_source_resolver` + `test_desktop_run_context` + `test_desktop_run_scope_cancel` + `test_desktop_execution_target_wire` + `test_desktop_local_root` + `test_desktop_local_context` + `test_desktop_contracts` + `test_desktop_gateway` + `test_desktop_file_access` + `test_effective_capabilities`） | **356 项通过** |
| 调度/自进化/fork/通道子集（`test_scheduler_*.py`、`test_evolution*`、`test_fork_multipart_agent_scope`、`test_private_agent_web_gate_ordering`、`test_agent_web_management`、`test_chat_identity_context`、`test_desktop_web_session`） | **310 项通过** |

### 变异检查（证明用例真的能失败）

每次植入缺陷后**先清 `__pycache__` 再跑**（第一次做变异时踩到过坑：两个长度相同的
改动让 `.pyc` 的 mtime+size 校验通过，读到的是旧字节码，见下文"四"）。

| # | 植入的缺陷 | 目标套件 | 结果 |
|---|---|---|---|
| M1 | `turn_is_interactive` 忽略 scheduled/background/machine 标志 | 3.7 | **5 项失败** |
| M2 | `executing_agent` 让 host 压过 speaker | 3.7 / 本文件 | **4 项 / 1 项失败** |
| M3 | 停用检查与"项目工具全不可用"检查短路 | 3.7 | **8 项失败** |
| M4 | `detach_local_execution` 空操作 | 3.7 | **3 项失败** |
| M5 | `registry.get(name, require_enabled=False)` 改回 `True` | 3.7 | **1 项失败** |
| N1 | `run_local_cwd` 忽略本轮 `local_execution_refusal` | 本文件 | **2 项失败** |
| N2 | 本机来源重新接受绝对路径（去掉 isabs/`~` 判定） | 本文件 | **1 项失败** |
| N3 | `resolve_target_root` 不再比对冻结目录与活体条目 | 本文件 | **1 项失败** |
| N5 | `execution_target_scope` 不再发布解析出的目录 | 本文件 | **3 项失败** |
| N6 | 运行时把 `exec` 加进 `COMMANDS["ops"]` | 本文件 | **1 项失败** |
| N7 | 临时给 `preload.ts` 加一个 `execCommand` 通道 | 本文件 | **1 项失败**（改后已还原，`git diff` 为空） |

全部变异还原后两个套件分别 **37 项、19 项通过**。

## 四、本轮发现并修掉的两件事

1. **过期字节码会伪造变异结论**：`executing_agent` 的变异把
   `speaker_agent_id or host_agent_id` 换成 `host_agent_id or speaker_agent_id` ——
   两者**长度相同**，且还原与编译发生在同一秒内，CPython 的源文件校验
   （mtime 秒 + size）因此认为 `.pyc` 仍然有效，随后所有运行都在跑旧字节码。
   现象是"恢复源文件后用例仍然失败"。**结论：变异检查必须清 `__pycache__`**，
   否则既可能漏报（变异没生效）也可能误报（还原没生效）。本轮所有变异结论
   都是清理后重跑的。
2. **入口门禁需要真实花名册**：3.7 让消息入口按被寻址 Agent 复核资格后，
   `tests/test_desktop_run_scope_cancel.py::RunTargetScopeTests` 会失败 ——
   它此前在"没有任何 Agent 花名册"的环境里跑 `_handle`，此时该 Agent 属
   "未注册"，入口如实拒绝并把目录留空，这是**预期行为**（未注册/已停用的
   Agent 不得继续沿用目录授权）。修的是**测试环境**：该用例组现在安装一个包含
   被寻址 Agent 的花名册再跑 `_handle`，与真实部署形态一致；断言一条没放松。

## 五、明确边界（不勾选的部分）

- **五行里与目标解析无关的语义**：A08 的"旧卡片仍指 A"只用作用域标识符断言
  （工作卡片的渲染链路在第 9 组工件来源适配器里覆盖）；A30 的"共享 Skill/知识
  维护限制不丢失"只断言"进入本机作用域不改变工具策略"，共享资产的完整限制由
  `tests/test_skill_public_surface_scope.py`、`tests/test_shared_asset_prompt_guidance.py`
  承担，未在本文重复。
- **A03/A10/A31 的真实 UI 与安装包链路**：目录选择器、页面侧 IPC 的真实
  Electron 行为属第 2 组（`desktop/e2e/directory-selection.spec.mjs`）与第 10 组
  门槛；本文的"无页面通用 exec IPC"是**静态源码断言 + 入站命令契约闭集**，
  不是运行时渗透测试。
- **远端设备**：A31 的"机器主体"目前覆盖同机进程内的后台主体；远端执行通道
  （第 6 组）接通后需在设备侧重跑同一判定。
