# A29 / A30 / A31：兼容矩阵与旧行为回归（任务 10.4 / 10.5）

本文件记录**开发机可执行部分**的实跑结果。安装包内（签名后、随包 Python、真机）与
Windows 的同等验收属任务 10.1–10.3，**未做**，不在本文的主张范围内。

两条任务的边界先说清楚：

- **10.4（A29）** 问的是"新旧混合时会不会悄悄走错路"：旧服务器 + 新客户端、
  新服务器 + 旧客户端、双方都新但开关关闭或能力不足；
- **10.5（A30/A31）** 问的是"没开这个能力的人有没有被弄坏"：无项目、只读输入、
  关闭项目、普通 Web/Channel、定时任务，以及既有 Agent Registry / Bridge /
  ExecutionRun / scheduler 接口是否保持兼容。

---

## 一、本轮实跑（`.venv/bin/python -m pytest ... -q -p no:randomly` / `node --test`）

| 批次 | 覆盖 | 结果 |
| --- | --- | --- |
| `tests/test_desktop_compatibility_matrix.py` | **本轮新增**，A29 四行矩阵 + 只读授权跨组合不被升级 | **11 passed** |
| `tests/test_desktop_target_resolution_acceptance.py` | A30（4 项）/ A31（4 项） | 通过（含在下面 65 项内） |
| `tests/test_agent_registry.py`、`tests/test_evolution.py`、`tests/test_evolution_safety.py`、`tests/test_evolution_trigger.py` | Agent Registry 与 ExecutionRun/演化链 | **65 passed, 16 subtests** |
| `tests/test_scheduler_*.py`、`tests/test_external_scheduler_boundary.py` | scheduler 全量接口 | **312 passed, 16 subtests**（含上表两行） |
| `tests/test_desktop_local_context.py`、`tests/test_shared_agent_personal_workspace.py`、`tests/test_multi_agent_runtime.py`、`tests/test_channel_double_start.py`、`tests/test_channel_instances.py` | Agent Bridge 消费方与 Channel 启动 | **110 passed** |
| `tests/test_desktop_meta.py`、`tests/test_desktop_execution_v2_contract.py`、`tests/test_desktop_release_gates.py`、`tests/test_desktop_compatibility_matrix.py`、`tests/test_desktop_project_migration_drill.py`、`tests/test_desktop_artifact_source.py` | 本 change 自身回归 | **155 passed, 56 subtests** |
| `node --test tests/test_desktop_execution_contract.cjs tests/test_desktop_device_client.cjs tests/test_desktop_core_integration.cjs` | 客户端侧契约与设备客户端 | **51 pass / 0 fail** |

（`test_channel_startup_open.py` / `test_external_channel_propagation.py` /
`test_weixin_qr_flow.py` 按既有约定会读本机真实部署数据，本批**刻意不纳入**，
以免把它们的环境依赖误记成兼容性结论。）

---

## 二、A29 四行矩阵（任务 10.4）

矩阵由 `tests/test_desktop_compatibility_matrix.py` 驱动，读的是**真实端点**
`GET /api/desktop/meta`（真实 WSGI app），开关通过 patch `web_channel.conf` 驱动、
验收状态通过就地改真实注册的 slice 并用 `try/finally` 还原，因此测的是
**组合后的答案**而不是某个 helper 的返回值。

| 组合 | 断言 | 结果 |
| --- | --- | --- |
| ① 旧服务器 + 新客户端 | 载荷没有 `project_execution` 块 → `negotiate()` 给 `(False, "not_implemented")`；`None` / `{}` / 只有 `bridge` 三种形状都算"没有" | 通过 |
| ② 新服务器 + 旧客户端 | 端点必发 `protocols.project_execution` 且 `required=False`；`web_session`/`bridge` 仍是 `1.0`；v1 的 `negotiate` 不受影响（`problems == []`、不把 `project_execution` 记进 `disabled`）；v1 帧不得自称 v2 | 通过 |
| ③ 双方都新、开关关闭 | 验收已在但部署未 opt-in → `disabled_by_deployment`（不是 `not_accepted`）——运维据此知道要动的是**自己的开关** | 通过 |
| ④ 双方都新、能力不足 | 代码在、批次未验收 → `not_accepted`，且 `state.configured == True`（**排除**"其实是开关问题"的误判）；`implemented=False` 时优先报 `not_implemented` | 通过 |

**跨四行的一条硬性质**（`test_a_client_never_falls_back_from_v2_to_v1`）：
v1 的 op 面（`contracts/desktop/v1.json` 的 `commands.ops` = list / stat / search /
read_text / materialize / inspect）**不含任何写工具**，v2 的写工具
（`required − readonly` = read/write/edit/ls/search_files 里的写子集与 bash）
只由 v2 声明。所以"新通道不可用"**没有**一条老通道可以退——那正是 A29 要禁止的降级，
因为退回去执行的是**写**动作，而老通道只承诺过读。

**交付态自检**：`desktop_project_execution_enabled` 与 `desktop_project_scripts_enabled`
出货为 `False`，默认部署下 meta 报不可用且理由在 `{not_accepted, disabled_by_deployment}` 内。

### 2.1 与既有结论的衔接

- 「旧只读 grant 不被升级」另有专门证据：任务 11.3 的
  `tests/test_desktop_release_gates.py::NoSilentEscalationOnUpgradeTests`（旧 target 读回
  `readonly-input`、不委派、拿不到本机目录、`workspace_id` 不会被当路径）；
  本文新增的 `ReadOnlyGrantsSurviveEveryCombinationTests` 把这四条在**同一个矩阵文件**里
  再跑一遍，其中
  `test_a_read_only_target_is_never_delegated_even_with_the_switch_open`
  把开关**强行置开**后仍返回 `False`，用来钉住"开关决定*能不能*委派，绝不决定*授了什么权*"。
- 「无 exec 降级到 v1」在客户端侧另有一份独立断言：
  `node --test tests/test_desktop_execution_contract.cjs` 的
  `an older server leaves the entry point unavailable, never downgraded`。

### 2.2 变异检查：这四行断言不是空转

矩阵类结论最容易写成「恰好都通过」的空断言——**本轮就真的抓到一条**。
`evidence/scripts/mutate_compatibility_matrix.py`（日志
`evidence/compatibility-matrix-mutations.log`）对 6 处实现走样逐一改写，
要求对应用例必须失败：

| # | 走样（都是"更省事、看起来更合理"的写法） | 被哪条用例抓住 |
| --- | --- | --- |
| C1 | 旧服务器（payload 里没有 `project_execution` 块）判为可用 | `test_row_1_an_old_server_leaves_the_entry_point_unavailable`（及 v2 契约侧同名用例） |
| C2 | 协议主版本不匹配时不再拒绝（"版本不同但先试试"） | `test_a_mismatched_major_is_a_protocol_refusal_not_a_fallback` |
| C3 | 服务端把新协议声明成 `required: True` | `test_row_2_an_old_client_keeps_reading_v1_and_is_not_offered_v2` |
| C4 | 原因判序把 `not_accepted` 排在 `not_implemented` 之前 | `test_row_4_a_missing_implementation_is_blamed_first`、`test_the_reason_names_the_first_missing_condition` |
| C5 | **把写工具加进 v1 的 `commands.ops`** | `test_a_client_never_falls_back_from_v2_to_v1` |
| C6 | 只读目标照常委派（开关决定授了什么权） | `test_a_read_only_target_is_never_delegated_even_with_the_switch_open` |

C5 是最值得留的一条：A29 禁止"新通道不可用就退回老通道"，**唯一理由**是老通道只承诺过读。
这条变异把 `bash` 加进 v1 的 op 表——如果用例只检查"v1 还能用"而不检查"v1 里没有写工具"，
`available=False` 就会悄悄变成"用老方式执行这次**写**"，而套件全绿。

**这次检查改强了一条用例（不是实现）**：`test_row_4_a_missing_implementation_is_blamed_first`
原先只取 `implemented=False, accepted=True`。此时两个条件并**不同时**未满足，
于是**任何**判序都会答 `not_implemented`——把优先级写反它照样通过，是典型的空转断言。
现已对 `accepted` 取 `(True, False)`；只有 `accepted=False` 那组能区分先后（C4 验证了这一点）。

结构上，本脚本已登记进 `tests/test_mutation_evidence_scripts.py` 的脚本表，
因此"锚点必须命中、必须唯一、只能落在登记的源文件上、每条必须写明期望失败"
这四项保证同样适用于它。

---

## 三、A30 / A31（任务 10.5）

### 3.1 A30：无项目、只读输入、关闭项目后

`tests/test_desktop_target_resolution_acceptance.py::A30NoProjectCompatibilityTests`（4 项）：

| 断言 | 要点 |
| --- | --- |
| 无项目的会话**不是拒绝** | `run_local_cwd` 返回 `(None, None)`（无拒绝原因）、`authorize_local_run` 放行、来源仍是服务器根 |
| 进入本机作用域**不改工具策略** | 作用域前后 `is_tool_allowed("knowledge_write")` 都是 `False`——共享资料/知识的维护限制**没有丢** |
| 只读 grant **不能被读成执行** | claim 成 `project-execution` 仍返回 `(None, REFUSAL_UNAVAILABLE)` |
| 关闭项目**回到默认** | `execution_target_scope` 变成 no-op，identity 与开项目前一致 |

补充：开发机侧 A30 的另一半（无项目/关闭项目后回归**本人工作区**、只读输入"读得到、
写与脚本被拒"）在 `evidence/local-e2e.md` 第 6 节由 `MasterBehaviourPreservedTests` /
`GrantModeTests` 覆盖。

### 3.2 A31：后台主体与页面传参

`A31NoBackgroundGrantTests`（4 项）：

| 断言 | 要点 |
| --- | --- |
| 同一轮只有**用户发起的**才放行 | `scheduled` / `background` / `machine_subject` / `task_source=scheduler` 四种触发全部 `REFUSAL_NEEDS_INTERACTIVE`；**不伪造 Membership** |
| 运行上下文同样拒绝后台轮 | 拒绝发生在本机目录解析之前 |
| 页面**不能**给 bridge 递 shell 或模块 | 传参被拒 |
| preload 面**没有**命令通道 | 不存在页面通用 exec IPC |

### 3.3 旧接口兼容（10.5 的"核对"部分）

按 acceptance 逐项核对，全部通过（见第一节实跑）：

| 接口 | 覆盖 | 结论 |
| --- | --- | --- |
| Agent Registry | `tests/test_agent_registry.py` | 未改语义 |
| Agent Bridge | `tests/test_desktop_local_context.py`、`tests/test_shared_agent_personal_workspace.py`、`tests/test_multi_agent_runtime.py`（`bridge/agent_bridge.py` 的真实消费方） | 未改语义 |
| ExecutionRun / 演化链 | `tests/test_evolution*.py` | 未改语义 |
| scheduler | `tests/test_scheduler_*.py`、`tests/test_external_scheduler_boundary.py` | 未改语义；且 A31 确认定时任务**不会**因此获得本机执行授权 |
| 普通 Web | `tests/test_desktop_meta.py`（公开端点走真实 WSGI app）、`tests/test_desktop_execution_v2_contract.py` | 未改语义 |
| Channel | `tests/test_channel_double_start.py`、`tests/test_channel_instances.py` | 未改语义 |

**注意**：这里的"未改语义"是**本批回归在开发机上通过的结论**，不是"对这些接口做过
针对性改造"——本 change 没有改这些接口，回归的作用是证明改动没有波及它们。

---

## 四、明确未完成（不作完成性主张）

- **安装包内**（macOS 签名件 + 随包 Python）的 A29/A30/A31 复跑：属 10.2；
- **Windows**：属 10.3；
- **真实模型**：本文件的"客户端"是产品自身的协商器与设备客户端，
  "服务器"是真实 WSGI app；真模型的工具消息属 10.2/10.3；
- 支持表上其他发布平台的矩阵：属 10.3。
