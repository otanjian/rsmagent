# 谁有权在本机项目里执行（任务 3.7）

对应主规范 `desktop-project-execution` 的「每轮复核实际执行者」与
「非交互触发不得继承本机项目授权」，以及 `execution-isolation` 的前置门槛。
本文记录**实际改了什么、怎么验证的、哪里没做**。

## 一、问题

3.5 把「这一轮在哪个目录」冻结成值，3.6 让所有引用走同一个来源解析器。两者都
假定「谁在跑」已经有答案，而实际上有两个位置会让答案变味：

1. **团队接续**：会话被指派给同事（`speaker_agent_id != host_agent_id`）时，
   真正执行工具的是**发言的那个 Agent**。如果沿用主持 Agent 的本机项目授权，
   就等于「在别人的对话里换了个人，却把项目一起递过去」——这正是需求禁止的自动授予。
2. **缓存实例复用**：`_agent_instances` 里的 `Agent` 带着上一轮的工作目录和
   `execution_target`。scheduler 触发、空闲自进化扫描捡起同一个实例时，
   会继承一次它从未获得过的交互授权，还会顺带在设备上唤醒执行。

判定依据刻意**不放在会话上**：会话是共享的，授权必须跟着「这一轮是谁在跑」和
「这一轮是什么性质的触发」。

## 二、交付内容

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| eligibility 单点判定 | `agent/desktop_local/run_authorization.py::agent_local_eligibility` | 用 `agent.effective_capabilities`（装配与派发用的同一套）判 allow/deny，不复制第二份规则；`read/write/edit/bash/ls/search_files` 全不可用即不合格，`skills` 显式选择不含目标技能即不合格 |
| 停用 Agent 单独报因 | 同上（`registry.get(name, require_enabled=False)`） | 停用要**能被找到**再报「已停用」，而不是混进「读不到」；两种原因的运维动作不同 |
| 交互性判定 | `run_authorization.py::turn_is_interactive` | 标志（scheduled/background/machine_subject）优先于字符串；`NON_INTERACTIVE_SOURCES` 只列已知非交互来源，原生消息不带来源即交互 |
| 执行者 = 发言者 | `run_authorization.py::executing_agent` | 指派给同事时发言者胜出：跑的是它的工具，算的是它的资格 |
| 统一入口 | `run_authorization.py::authorize_local_run` | 入口解析只传 `host_agent_id`，回复路径传两者；两条链不会漂移 |
| 本机执行拒绝位 | `common/runtime_identity.py::RuntimeIdentity.local_execution_refusal`、`override_identity/restore_identity` | 只在**本轮**身份上收窄，不动会话与共享对象 |
| 拒绝优先于一切 | `agent/desktop_local/run_context.py::run_local_cwd` | 有拒绝位即拒绝，且**不**回落服务器目录 |
| 入口按实际发言者复核 | `channel/web/fork/execution_scope.py::execution_target_scope` | 不合格的 Agent 拿到目标但拿不到目录（`execution_cwd=None`+原因） |
| 装配时不同步项目 | `bridge/agent_bridge.py::_apply_session_project` | 用 `agent_local_eligibility(actual_agent_id)` 判实际执行的 Agent；不合格只标 `mark_local_context_unavailable`，不把目录装到它的工具上 |
| 回复前拒绝并摘除缓存 | `bridge/agent_bridge.py::agent_reply`（+`_session_has_local_project`、`_detach_cached_local_project`） | 非交互触发且会话**确实**指向本机项目 → 直接回 `REFUSAL_NEEDS_INTERACTIVE`，并把缓存实例上的项目摘掉（`detach_local_execution`）后才返回；不合格的发言者改走 `narrow_local_execution`，在 `finally` 里 `restore_identity` |
| 后台复用不带目录 | `agent/evolution/executor.py` | 空闲自进化扫描取用缓存实例前先 `detach_local_execution(agent, REFUSAL_BACKGROUND_CACHED)` |
| 调度触发自报来源 | `agent/tools/scheduler/integration.py` | 定时任务上下文显式带 `task_source='scheduler'`，不依赖字符串在每一跳存活 |

「拒绝只拒绝需要目录的调用」不是新写的：`run_context.py::needs_local_directory`
已经在 3.5 定义，本轮只是让它也认拒绝位。因此同事的 `memory_search` 照常可用。

## 三、验证

命令固定为 `.venv/bin/python -m pytest <子集> -q -p no:randomly`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_run_authorization.py` | **37 项 + 3 子测试通过** | `AgentLocalEligibilityTests`(9)：普通 Agent/仅一个项目工具/无技能选择可执行；空 allowlist 无项目工具、denylist 覆盖全部项目工具、停用、未注册、技能未选中、技能选择含目标 → 各自按因拒绝。`TurnKindTests`(7)：调度/后台/机器主体三分支 + 四个来源串全部拒绝，原生消息与空来源不动。`RunLocalCwdRefusalTests`(3)：拒绝位优先于目标目录、无目标无影响、服务器目录不接管。`NarrowLocalExecutionTests`(2)：收窄只改本轮身份、无本机目标不动作。`DetachLocalExecutionTests`(3)：清目标+清原因、无方法替身走两步、None 安全。`EntryScopeGateTests`(3)：入口按实际发言者复核。`ApplySessionProjectGateTests`(3)：不合格时不把目录装到工具上、合格时保持原行为。`AgentReplyGateTests`(6)：调度触发**在触碰 Agent 前**即拒绝且缓存实例被摘除、后台来源同样拒绝、无本机项目的会话不被牵连、合格交互轮次与身份**逐字段不变**、无项目工具的同事被收窄但仍能回答、收窄在轮次结束后撤销。`SchedulerSourceTests`(1)：定时任务上下文实际带 `task_source='scheduler'` |
| 桌面子集合跑：`test_desktop_run_authorization.py test_desktop_source_resolver.py test_desktop_run_context.py test_desktop_contracts.py test_desktop_execution_target_wire.py test_desktop_local_root.py test_desktop_local_context.py` | **260 项通过** | 3.5/3.6 的既有断言未被本轮改动破坏 |
| 调度/自进化/fork 子集：`test_scheduler_*.py test_evolution.py test_evolution_safety.py test_evolution_trigger.py test_fork_multipart_agent_scope.py test_private_agent_web_gate_ordering.py test_agent_web_management.py` | **272 项通过** | 接入点（scheduler 集成、自进化执行器、fork 作用域）既有行为不变 |

### 变异检查（证明用例真的能失败，而不是碰巧通过）

在 `agent/desktop_local/run_authorization.py` 上逐条植入缺陷，跑同一个套件，
每次记录失败数后还原源文件（备份 `/tmp/ra.bak`）：

| # | 植入的缺陷 | 结果 |
|---|---|---|
| M1 | `turn_is_interactive` 忽略 scheduled/background/machine 标志 | **5 项失败** |
| M2 | `executing_agent` 让 host 压过 speaker（即接续仍用主持者资格） | **4 项失败** |
| M3 | 停用检查与「项目工具全不可用」检查都短路成 `if False` | **8 项失败** |
| M4 | `detach_local_execution` 变成空操作 | **3 项失败** |
| M5 | `registry.get(name, require_enabled=False)` 改回 `True`（停用被并进「读不到」） | **1 项失败** |

还原后同一套件 **37 项通过**，与变异前一致。

## 四、明确边界（不勾选的部分）

- **真实调度器端到端**：本文只证明「带 `task_source='scheduler'` 的上下文在回复路径
  被拒绝」，没有让真实 scheduler 在真实设备上触发一次本机项目任务；第 5/6 组的
  本机端到端与本机模式接通后再补。相关既有用例见 `tests/test_scheduler_task_authorization.py`。
- **技能级资格的运行时判定**：`skill=` 参数已实现并有单测，但「这一轮实际要跑哪个
  技能」在 `agent_reply` 阶段还未确定，因此回复路径目前只传 Agent 级资格；
  技能级拒绝适用于调用点已知技能名的路径（第 8 组技能包）。
- **远端设备的同一判定**：本机项目经由设备命令执行（第 6 组）尚未接通，因此
  「谁在执行」目前只覆盖同机进程内的 Agent；远端接入时必须在设备侧复用同一入口。
- **未覆盖的既有失败**：本次子集内无既有失败；`tests/test_weixin_qr_flow.py` 等
  环境依赖套件未纳入（见规则 `targeted-test-runs`）。
