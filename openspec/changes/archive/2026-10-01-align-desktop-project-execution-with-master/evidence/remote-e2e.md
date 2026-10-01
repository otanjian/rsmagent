# v2 项目执行：远程命令通道与双端授权证据（任务组 6）

本文件按任务组 6 的条目累积记录。每条只写**实际执行过**的命令、原始结果和变异（mutation）验证；未完成的验收项明确标注，不用「间接推断」顶替。

环境：macOS（`posix`），仓库根 `/Users/jiantan/ai_assistant/rsmagent`，测试用 `.venv/bin/python` 与 `node --test`。

---

## 6.1 独立执行 v2 schema / 样例 / 生成类型 + meta/hello 能力交集

### 落点

| 产物 | 作用 |
| --- | --- |
| `contracts/desktop/v2.json` | v2 契约的唯一数据源。**未**修改 `v1.json`，也**未**扩大 v1 的 `commands.ops`（只读语义保持原样）。 |
| `auth/desktop_contracts_v2.py` | Python 侧唯一读取器与校验器：协商、`execute_tool` 信封、结果/工件/开始许可/journal 形状、阶段与效果投影、能力块形状、跨文档一致性。 |
| `scripts/gen_desktop_execution_types.py` | 由契约生成客户端常量与类型（"生成类型"落实为真生成 + 陈旧即失败）。 |
| `desktop/src/main/project-execution/generated-contract.ts` | 生成产物（`do not edit`）。 |
| `desktop/src/main/project-execution/contract.ts` | 客户端行为层：只保留不可生成的规则（发送前校验、阶段投影、协商）。 |
| `integrations/desktop/execution_capability.py` | 组装 meta/hello 上报的能力交集（声明 × 部署开关 × 平台）。 |
| `channel/web/fork/handlers/desktop.py` | `GET /api/desktop/meta` 增加可选 `project_execution` 块与 `protocols.project_execution`。 |
| `config.py` / `auth/capability_matrix.py` | 新增 `desktop_project_execution[_scripts]` 切片（`implemented=True, accepted=False`）与两个默认关闭的部署开关。 |

契约要点（与 `execution-contract.md` §1–§7 对应）：

- `project_execution` 协议 `major=2`、`required=false`：v1 只读端与新端两个方向都保留 v1；缺少 v2 时入口**明确不可用**，绝不降级成 v1 的 `inspect`/`materialize`。
- 阶段是**投影**：`queued/dispatched/acknowledged/running/succeeded/failed/cancelled/expired` → 九个可见阶段；`cancelling` = `running` + 取消请求；`outcome_unknown` = `failed` + `code=outcome_unknown`。`next_command_state('cancelling') == 'running'`，`next_command_state('outcome_unknown') is None` —— 不会把数据库不认识的状态字符串写进去。
- 效果不许乐观：`cancelled → unknown`（不承诺回滚）、`failed`（无具体错误码）`→ unknown`、`permission_denied → none`、`deadline_exceeded → partial`、`succeeded → completed`。
- 信封禁字段：顶层与 `arguments` 内均禁止 `cwd/root_path/absolute_path/env/server_url/authorization/token/module/class_name/argv_path`。
- 限额不重复声明：v2 用 `limits.reuse_v1` 指名继承 v1 的 7 个界限，`contract_problems()` 会拒绝任何"两边都写"的界限。
- 新错误码只增不覆盖：v1 已定义的码（`permission_denied`/`stale_context`/`device_offline`/`limit_exceeded`/`deadline_exceeded`/`feature_unavailable` …）仍以 v1 为准，v2 只加 `approval_required`/`command_conflict`/`outcome_unknown`/`runtime_unavailable`/`incompatible_skill`/`resource_unavailable`/`platform_unsupported` 等。
- Windows 诚实上报：`platforms.win32.supported=false`，工具集为空并给出 `platform_unsupported`，不把 POSIX 命令翻译后执行。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_execution_v2_contract.py \
      tests/test_desktop_execution_types.py tests/test_desktop_contracts.py \
      tests/test_desktop_meta.py tests/test_desktop_web_session.py \
      tests/test_desktop_identities.py tests/test_desktop_target_resolution_acceptance.py \
      tests/test_desktop_local_context.py -q -p no:randomly
187 passed, 16 subtests passed in 10.44s

$ node --test tests/test_desktop_execution_contract.cjs
ℹ tests 14   ℹ pass 14   ℹ fail 0

$ .venv/bin/python scripts/gen_desktop_execution_types.py --check
generator --check: clean
```

`GET /api/desktop/meta`（真实 WSGI 应用，无凭据）现在的相关输出：

```json
{"protocols": {"web_session": {"major": 1, "minor": 0},
               "project_execution": {"major": 2, "minor": 0, "required": false}},
 "project_execution": {"available": false, "reason": "not_accepted",
                       "protocol_major": 2, "required": false,
                       "tools": ["read", "write", "edit", "ls", "search_files", "bash"],
                       "platform": "posix", "runtime": "cpython-3.11",
                       "files_write_verified": false, "scripts_verified": false,
                       "surfaces": {"files": {"available": false, "reason": "not_accepted"},
                                    "scripts": {"available": false, "reason": "not_accepted"}}}}
```

即：能力**尚未验收**时 meta 报告 `not_accepted`，`files_write_verified`/`scripts_verified` 均为 false；v1 的 meta 信封校验（`desktop_contracts.validate_meta`）在接受新增块后仍为 `[]`。

### 变异验证（每条都先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| 契约里把 `succeeded` 改名为 `done`，不重新生成 | 两侧同时报警 | `test_desktop_execution_types.py` 3 failed（陈旧文件、`--check`、阶段名）；`contract_problems()` 返回 `["phase 'done' is never reachable"]` |
| 把继承来的 `pending_queue` 在 v2 里重复声明 | 跨文档一致性拒绝 | `contract_problems()` 返回 `["limit 'pending_queue' is restated here and reused from v1"]`；`test_the_two_documents_agree` / `test_v1_is_not_widened` 2 failed |
| 手改生成文件（`script_timeout_max_seconds: 9000`） | 陈旧检查失败 | 3 failed（含"限额不是来自两份文档"一条） |
| 还原后复跑 | 全绿 | `65 passed`（两个契约测试文件） |

### 验收对应（部分，完整版见 6.8）

- A05/A09/A10/A17/A28 的**共同前提**（协议协商、能力交集、关闭态报告、v1 兼容）在本条落地并有测试；这些验收**尚未**证明：真实模型调用六工具（A05）、两处重验（A09/A10）、脚本真实退出与后台句柄（A17）、审计与配额（A28）。对应任务 6.3–6.8。
- 「新读写/执行能力不绕过治理切片」：两个切片 `implemented=True` 但 `accepted=False`，且部署开关默认关闭；`test_the_switch_alone_cannot_open_the_capability` 证明**打开开关也不会**让能力变为可用。

---

## 6.2 复用 commands / outbox / 连接租约承载 v2 payload

### 落点

| 产物 | 作用 |
| --- | --- |
| `auth/store.py`（`_migration_42`） | `desktop_commands` 增加 v2 列（`protocol_major`/`tool_name`/`tool_schema_version`/`run_id`/`tool_call_id`/`selection_generation`/`origin`/`params_digest`/`execution_phase`/`effects`/`cancel_requested`/`journal_id`/`permit_id`/`started_at`/`heartbeat_at`/`approval_id`/`permission_mode`），并新建 `desktop_execution_permits` 表。 |
| `integrations/desktop/commands.py` | 复用**同一个** outbox 单写者：`create_execution` / `create_execution_for_context` / `issue_start_permit` / `record_start_intent` / `record_heartbeat` / `request_cancel` / `complete_execution`。 |
| `integrations/desktop/execution_payload.py` | 规范信封（14 个固定字段）的 sha256，与客户端逐字节一致；`device_execution_frame` / `validate_device_result`。 |

**保留既有状态机**：v2 的写路径只走既有的 `queued → dispatched → acknowledged → running → succeeded/failed/cancelled/expired` 列，没有新增状态字符串；`execution_phase`、`effects`、`cancel_requested` 是**投影字段**，不参与状态机。旧端（v1 只读）读到的 `desktop_commands` 行布局仍然是原有的列 + 新增可空列。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_execution_commands.py \
      tests/test_desktop_execution_payload.py tests/test_desktop_execution_v2_contract.py \
      tests/test_desktop_execution_types.py -q -p no:randomly
108 passed in 2.66s

$ node --test tests/test_desktop_execution_contract.cjs
ℹ tests 19   ℹ pass 19   ℹ fail 0

$ .venv/bin/python -m pytest tests/test_desktop_gateway.py tests/test_desktop_migration_drill.py \
      tests/test_desktop_local_e2e.py tests/test_desktop_execution_target_wire.py \
      tests/test_desktop_phase2_gates.py tests/test_desktop_file_access.py \
      tests/test_desktop_transfer.py tests/test_desktop_run_authorization.py \
      tests/test_desktop_run_scope_cancel.py -q -p no:randomly
160 passed, 3 subtests passed in 8.31s
```

### 变异验证（每条都先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| 客户端 `canonicalJson` 不再按码点排序 key（`contract.ts`，重编译 TS） | 客户端互认测试失败 | `node --test` → **17 pass / 2 fail**（"computes the same params_digest as the server"、"key-order independent"）；还原后 19/19 |
| 服务端 `execution_payload` 把 `separators=(",",":")` 改成带空格的 `(", ", ": ")` | Python 侧摘要与共享夹具脱钩 | `test_desktop_execution_payload.py` → **3 failed, 18 passed**（`test_the_shared_fixture_matches_the_python_implementation`、`test_the_fixture_is_stable_under_key_order`、`test_the_canonical_text_is_pinned_across_languages`）。此时 `node --test` 仍 19/19 —— 说明两侧各自钉住同一夹具，任一侧漂移都会在**它自己那一侧**报错，不可能"双端都改才被发现" |

摘要一致性由**共享夹具**（`contracts/desktop/samples/v2/digest_envelope.json`）双向钉住：Python 与 TS 各自与夹具比对，因此不存在"两端一起改就无声通过"的窗口。

### 尚未证明（留给后续条目）

- 6.2 只证明**单写者与字段**落地；真正把 `agent_stream` 的工具调用接到这条通道上是 6.3。
- 配额预留/释放与启动/终态审计是 6.7；`desktop_execution_permits` 表已建、`issue_start_permit` 已实现，但**消费**许可的两个校验点（创建、实际开始）在 6.5。

---

## 6.3 `agent_stream` 工具门禁之后接入桌面代理

### 落点

| 产物 | 作用 |
| --- | --- |
| `agent/protocol/agent_stream.py::_run_tool` 之后的 `_remote_call` | 决定「这次调用跑在哪台机器」。它在**既有权限门禁之后**运行：`_permission_denial`、配额与模式判定先照旧执行，设备分支只在门禁放行后追加 |
| `agent/desktop_remote/dispatch.py::plan` | 返回三种结果：`None`（不是远程调用）、`refusal`（设备自己的码）、代理工具 |
| `agent/desktop_remote/proxy_tool.py` | 每次调用现构的代理工具，沿用 master 的 `name`/`params`/`ToolResult`，不复制工具业务实现，也**不**把 Agent 的 `self.tools[name]` 改指向设备 |

**保持原语义**：代理工具的名字与 schema 与被代理的主工具逐字相同（`_remote_call` 用例断言 `first.tool.name == "read"`）；`run_id`/`tool_call_id` 由 `ExecutionRun` 原样带入命令行；真实结果（stdout、退出码、截断、artifacts）经 `result_for` 投影回原 `ToolResult`，不在这里做任何「成功化」。

**只在授权时追加，绝不兜底**：`plan` 只对「本进程够不到的本机项目 + 已打开的执行开关 + 设备已声明该工具」生效；`current_identity().execution_target` 为 `None`、来源为只读、工具属服务器侧（记忆/知识/API 客户端）时一律返回 `None`，**原有分支不变**。规划异常被吞掉并记 warning，退回原行为，不凭空发明一台设备。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_remote_dispatch.py -q -p no:randomly
92 passed in 15.23s

$ .venv/bin/python -m pytest tests/test_desktop_process_handles.py \
      tests/test_desktop_execution_v2_contract.py tests/test_desktop_execution_commands.py \
      tests/test_desktop_execution_broker.py tests/test_desktop_remote_dispatch.py -q -p no:randomly
219 passed in 22.42s
```

其中 `RemoteDispatchTests` 33 项（含 `test_the_whole_remote_call_returns_the_real_result`、
`test_a_server_side_tool_is_not_delegated`、`test_a_readonly_reference_does_not_delegate_the_call`、
`test_the_proxy_is_built_per_run_and_never_adopted_by_the_agent`、
`test_an_offline_device_refuses_the_call_before_anything_is_queued`、
`test_a_tool_the_device_did_not_declare_is_a_capability_refusal`）。

### 变异验证（先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `_remote_call` 去掉 `remote_mode_for(identity)` 守卫（关闭态/只读态也会委派） | 关闭开关的调用不再回落 | `test_the_switch_closes_the_delegation_without_touching_the_local_mode` **2 failed**（`RemoteDispatchTests` 与继承它的 `BackgroundHandleTests` 各一处）；还原后全绿 |

---

## 6.4 主进程 broker 的窄化四端点

### 落点

| 产物 | 作用 |
| --- | --- |
| `integrations/desktop/execution_broker.py` | `prepare` / `start` / `heartbeat` / `status` 的唯一实现；每个端点重跑同一套检查 |
| `channel/web/fork/handlers/desktop.py` | 四条路由（`POST prepare/start/heartbeat`、`GET status`）的 HTTP 外壳 |

**绑定而不是信任**：每条请求都绑定 owner/设备/binding/项目/`grant_version`/调用摘要（`params_digest`），并逐次重跑：`require_live_epoch`（活租约、当前代次）、`_owned_row`（本人、本租户、本设备）、`resolve_target_root`（项目仍授权且目录仍在）。跨作用域轮询返回 `resource_not_found`/`stale_context`，不会被「猜 command_id」拿到。

**页面拿不到通用执行**：body 深度拒绝（`broker.forbidden`）任何 URL、`authorization` 头、`module`/`class_name`、`cwd`/根路径；凭据必须是 native bearer —— Cookie 页面会话在授权之前即 `401`；`GET status` 与 `POST *` 走同一组标识检查。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_execution_broker.py -q -p no:randomly
39 passed in 4.4s      # BrokerEndpointTests 37 + ExecutionGateTests 2
```

### 变异验证（先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `_check_row_scope` 不再比对 row 的 binding/workspace/device/grant（只认 digest） | 跨作用域轮询不再被拒 | `.venv/bin/python -m pytest tests/test_desktop_execution_broker.py -q -p no:randomly` → **2 failed, 37 passed**（`test_status_refuses_a_command_the_caller_cannot_continue`、`test_prepare_refuses_a_workspace_the_command_does_not_belong_to`）；还原后 39/39 |

> 页面凭据（Cookie → 401）由 `test_a_page_session_cannot_prepare_an_execution` / `test_a_page_session_cannot_read_the_status` 正向断言（`_open` 的 `require="native"`），不再单独做变异。

---

## 6.5 创建与实际开始两处重验，许可限时单次

### 落点

`**两次**独立重验`：`prepare` 与 `start` 各自跑一遍 membership/tenant 活性、`Agent.use`、工具权限策略（`agent.permission.policy`）、单动作审批状态、`quota_available`（只读重查）、项目 grant 版本与平台声明。任一侧不满足即在**副作用之前**失败，命令不推进。

**开始许可**（`desktop_execution_permits`）只由 `start` 签发，TTL 取自契约（≤10s），一次性：消费为 `used_at` 的 CAS；过期/已用/跨命令分别 `permit_expired`/`already_started`/`invalid_request`。`start` 必须在活租约纪元下进行（离线 `device_offline`、旧纪元 `stale_context`）。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_execution_broker.py \
      tests/test_desktop_execution_commands.py -q -p no:randomly
61 passed
```

### 变异验证（先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `issue_start_permit` 默认 TTL 改为固定 3600，并去掉「不得超过契约上限」守卫 | 长命许可可被签发 | `.venv/bin/python -m pytest tests/test_desktop_execution_broker.py tests/test_desktop_execution_commands.py -q -p no:randomly` → **9 failed, 52 passed**（含 `test_the_permit_is_short_lived_and_single_use`）；还原后 61/61 |

> `start` 前撤权/配额耗尽/审批撤销由 `test_start_re_validates_the_authorization_prepare_answered` 一族正向断言；`permit_expired`/`already_started` 各有用例直接构造对应的许可状态。

---

## 6.6 后台 Bash 句柄绑定设备/项目/运行

### 落点

| 产物 | 作用 |
| --- | --- |
| 迁移 43 `desktop_process_handles` | 设备返回的 `bash_id` 即句柄；记录 owner/租户/Agent/会话/设备/binding/项目/`grant_version`/`run`/`tool_call`、`expires_at` 与 terminated 状态 |
| `integrations/desktop/process_handles.py` | `record`（仅来自设备终态 payload，按 `(run_id, tool_call_id)` 幂等）、`resolve`（路由前重验作用域）、`close_for_scope`/`terminate_for_run`（撤权与取消时退休句柄） |
| `agent/desktop_remote/dispatch.py` | `plan`/`run_remote_tool` 派发前以 `background_refusal` 拒绝对外会话/跨项目/跨账户/过期句柄；kill 成功后按设备回执 `_close_background_handle(terminated=True)` |
| `auth/desktop_contracts_v2.py` | `bash` 参数形状补入 `bash_id`（读取/kill）；空 `command` 且无 `bash_id` 仍拒绝 |

读取与 kill 走**同一条** `execute_tool` 通道，复用主工具结果格式（`result.ext_data.source == "desktop"`）。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_process_handles.py -q -p no:randomly
8 passed

$ .venv/bin/python -m pytest tests/test_desktop_remote_dispatch.py::BackgroundHandleTests -q -p no:randomly
52 passed
```

`BackgroundHandleTests` 覆盖：句柄落到原设备/项目/运行、foreground 不建句柄、接续路由回原设备且复用主结果形状、他人会话不可用、跨项目/跨账户拒绝、过期句柄 `transfer_expired`、`run` 取消退休句柄、撤权退休句柄。

---

## 6.7 关联 ID、审计、配额释放与错误处理

### 落点

| 产物 | 作用 |
| --- | --- |
| `integrations/desktop/commands.py` | `desktop.execution.start`（`AUDIT_EXECUTION_START`）、`desktop.execution.terminal`（`AUDIT_EXECUTION_TERMINAL`）、`desktop.execution.outcome_unknown`（`AUDIT_EXECUTION_UNKNOWN`）三条审计都带上 `run_id`/`tool_call_id`/`journal_id`/`permit_id`/`connection_epoch`/`grant_version`；命令取消时 `_retire_run_handles` |
| `auth/service.py::refund_quota` | 释放是同一个 `quota_usage` 表的另一半，不是第二套真值：按 `(user_id, tenant_id, metric)` 扣减、不低于 0，并写 `quota.refund` 审计 |
| `agent/desktop_remote/dispatch.py::release_tool_call_quota` / `_release_unspent` | 委派调用**没跑成**时释放工具门禁的那一单位：规划拒绝、设备离线、句柄对外/过期、队列拒绝、开始前被取消 |
| `agent/protocol/agent_stream.py::_release_unspent_tool_call` | 远程规划拒绝时在同一条门禁表上释放 |

**原则上不重复扣/不重复退**：已经开始（`started_at` 有值、或设备已回终态）的调用**永不**退费——机器时间已经花出去了；退费只针对「预算没有买到任何机器时间」的分支。

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_remote_dispatch.py -q -p no:randomly
92 passed in 15.23s
```

配额相关断言都是**真实**的 `quota_usage` 行：`test_a_released_charge_fits_under_the_limit_again` 证明「退费后同样的限额又能放进 5 次、第 6 次仍超」；`test_a_release_never_creates_credit` 证明无扣减时不产生额度；`test_an_offline_device_gives_the_charge_back` 证明设备从没看到的调用不计费；`test_a_run_the_device_started_keeps_its_charge` 与新增的 `test_a_cancelled_command_that_had_started_keeps_its_charge` 证明已经开始的调用保留计费。

### 变异验证（先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| 终态释放条件去掉 `and not final.get("started_at")`（已开始的取消/过期也退费） | 已开始却退费应被拒 | `test_a_cancelled_command_that_had_started_keeps_its_charge` **1 failed**（本轮为此新增该用例补上原先的空档：旧用例只覆盖 `failed`+`started_at`，没有覆盖 `cancelled`+`started_at`）；还原后 92/92 |

> 陷阱记录：改完源文件必须先清 `__pycache__` 再跑，否则等长改动的 mtime+size 校验可能让旧字节码蒙混通过，得出「变异未捕获」的假结论。本轮第一次跑就踩到，清缓存后复现正确失败。

---

## 6.8 A05 / A09 / A10 / A17 / A28 编号级验收

### 落点

| 产物 | 作用 |
| --- | --- |
| `tests/test_desktop_remote_execution_acceptance.py` | 编号级套件（61 项，其中 A 编号新增 10 项）：A05 六工具走真实 `AgentStreamExecutor._execute_tool`、A10 远程委派读本轮拒绝、A17 真实终态与非本设备句柄、A28 磁盘不足与预留释放 |
| `evidence/governance.md` | A09/A10/A28 逐子项 → 用例映射（含未覆盖项） |
| `tests/test_desktop_execution_commands.py` | 新增 `test_an_audit_write_failure_does_not_silently_start_the_command`（A28 审计写入失败） |
| `agent/desktop_remote/dispatch.py` | **缺陷修复**：`plan` 在读设备前先读本轮的 `local_execution_refusal`（见下） |

### 运行结果

```
$ .venv/bin/python -m pytest tests/test_desktop_remote_execution_acceptance.py -q -p no:randomly
63 passed in 15.59s

$ .venv/bin/python -m pytest tests/test_desktop_execution_commands.py -q -p no:randomly
23 passed

$ .venv/bin/python -m pytest tests/test_desktop_remote_dispatch.py tests/test_desktop_remote_execution_acceptance.py \
      tests/test_desktop_process_handles.py tests/test_desktop_execution_broker.py \
      tests/test_desktop_execution_commands.py tests/test_desktop_execution_v2_contract.py \
      tests/test_desktop_execution_payload.py tests/test_desktop_execution_types.py \
      tests/test_desktop_target_resolution_acceptance.py tests/test_desktop_run_authorization.py \
      tests/test_desktop_run_context.py tests/test_desktop_run_scope_cancel.py \
      tests/test_desktop_source_resolver.py -q -p no:randomly
477 passed, 19 subtests passed
```

A05 的四条硬断言：六次调用各返回**设备**的答案（`DEVICE_ANSWERS`，服务器工具答不出
来）、`desktop_commands` 恰好新增 6 条、服务器 `server workspace` 的哨兵文件字节不变
且没有新文件落盘、master 工具未被改指向设备。A17 断言非零退出/截断文本原样返回且不含
「成功」、跨设备句柄 kill 被拒、前台超时按 `deadline_exceeded` 拒绝。

### 本轮修掉的一处真实缺陷（A10，远程模式）

`execution_target_scope` 会把本轮拒绝（非交互触发、无资格的实际执行 Agent）写进
`identity.local_execution_refusal`；本地路径在 `run_local_cwd` 据此拒绝，**远程路径此前
没有读它**，于是这类运行在本机项目不可达时仍被委派到用户设备上执行 —— 违反 A10 与
任务 3.7「不为 scheduler 或机器主体自动授予本机项目执行」。

修复：`dispatch.plan` 在设备检查前先读本轮拒绝并返回 `Plan(refusal=…,
kind="unavailable", code="permission_denied")`。先红后绿：修复前 2 项 A10 用例失败
（`planned.tool` 是代理工具），修复后 3 项全过；dispatch 套件 92 项无回归。

### 变异验证（先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `plan` 去掉 `local_execution_refusal` 分支 | A10 远程拒绝退化为放行 | 2 项 A10 用例失败；还原后 92/92 + 63/63 |
| `_record_start` 在写审记前先 `con.commit()` | 审计失败不再回滚 | `test_an_audit_write_failure_does_not_silently_start_the_command` 1 failed；还原后 23/23 |

### 诚实边界（不得当作已完成）

- **模型**是注入到 `_execute_tool` 的工具调用，不是字面 LLM；**网关/设备**帧由产品自身的
  测试助手产生。因此本条目证明的是「服务器侧的委派、治理与结果投影在真实接缝上成立」，
  即 P2 的**代码侧**闭环；**安装包内**的真机/真模型验收属于第 10 组（任务 10.2/10.3），
  未做。
- Windows 平台、脚本 worker 的真实沙箱在本条目不涉及（第 4 组，已单独记录）。
- A09 的 Membership/Agent.use 专门用例、A28 的真实 ENOSPC，见 `evidence/governance.md`
  的「未覆盖 / 明确不做」。
- **已知顺序相关抖动**：把多个各自新建 `WebAppHarness` 的桌面套件放进同一次 pytest
  进程时，配额类用例偶发失败（退费经由进程级 `get_identity_service()`，指向与当前
  `self.app` 不同的库）。该抖动在**未加载本 acceptance 文件**时同样出现，属既有跨套件
  顺序依赖，不是本次改动引入；单套件运行稳定可复现。
