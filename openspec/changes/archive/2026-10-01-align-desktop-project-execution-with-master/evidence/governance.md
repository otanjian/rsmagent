# v2 项目执行的治理切片证据（A09 / A10 / A28）

本文件把治理相关的编号验收（撤销、资格、审批/配额/审计）逐条映射到**真实执行过的
用例**上，并明确列出**未覆盖**的子项。所有引用都能在当前代码上复跑；套件名后括号
内是本轮实跑通过数。

覆盖本文件的套件（本轮实跑）：

```
$ .venv/bin/python -m pytest tests/test_desktop_execution_broker.py \
      tests/test_desktop_execution_commands.py -q -p no:randomly
61 passed   # 6.4/6.5 端点上位检查

$ .venv/bin/python -m pytest tests/test_desktop_remote_execution_acceptance.py -q -p no:randomly
63 passed   # A05/A10/A17/A28 编号级

$ .venv/bin/python -m pytest tests/test_desktop_run_authorization.py \
      tests/test_desktop_target_resolution_acceptance.py -q -p no:randomly
（见各自 evidence；A10 的本地与装配侧）
```

---

## A09 队列接收后、开始之前撤销授权

要求：对应命令在**副作用前**失败；不能借缓存技能或旧许可执行；错误与审计可关联。

| 撤销对象 | 用例 | 断言要点 |
| --- | --- | --- |
| 项目 grant（工作区撤权） | `test_desktop_execution_broker.py::test_prepare_refuses_a_revoked_workspace_grant`、`::test_start_re_validates_the_authorization_prepare_answered` | `prepare` 通过后撤权，`start` 仍返回 403 `grant_revoked`；start 是唯一签发点，因此没有许可被签出 |
| binding | `::test_prepare_refuses_a_revoked_binding` | binding 撤权后 `start`/`prepare` 均拒绝 |
| 设备 | `::test_prepare_refuses_a_disabled_device`、`::test_start_refuses_while_the_device_holds_no_live_connection`、`::test_start_refuses_a_superseded_connection_epoch` | 离线 `device_offline`、旧代次 `stale_context` |
| 开始许可过期 | `::test_a_permit_that_expired_cannot_start_anything`、`test_desktop_execution_commands.py::test_an_expired_permit_is_refused` | 过期许可不能推进状态，`state` 仍为 `acknowledged`、`started_at` 为空 |
| 许可一次性 | `test_desktop_execution_commands.py::test_a_permit_cannot_start_a_command_that_already_started` | 二次消费 `already_started` |
| 审计可关联 | `test_desktop_execution_commands.py::test_the_lifecycle_audit_records_start_and_terminal` | 审记带 `run_id`/`tool_call_id`/`journal_id`/`permit_id`/`connection_epoch`/`grant_version`，且**不含**路径/令牌/环境 |

「借缓存技能或旧许可执行」：技能资格在 `test_desktop_run_authorization.py`（技能未选中
→ 拒绝）覆盖；许可部分由上面「一次性 + 过期不可启动」覆盖。

**未覆盖**：Membership 行被删除、Agent.use 被撤销的**专门**用例（`_open` 路径会经
`require_tenant`/`verify_binding_scope` 拒绝，但没有一条只改这两项并断言具体错误码的
用例）。列为已知缺口，不用间接推断顶替。

---

## A10 按实际执行 Agent 的资格拒绝；代理不能跳过工具门禁

要求：团队会话转给无资格的 Agent 时按**实际执行 Agent** 拒绝；服务端代理工具被黑名单
禁止时不能绕过现有工具门禁。

| 层次 | 用例 | 断言要点 |
| --- | --- | --- |
| 本地/装配（既有） | `tests/test_desktop_run_authorization.py`（37 项 + 3 子测试） | 资格（停用/无工具/技能未选中）、触发性质（scheduler/background/machine）、身份收窄、缓存摘除、入口/装配/回复三层 |
| 编号级（既有） | `tests/test_desktop_target_resolution_acceptance.py` 的 A10 段 | 接续拒绝指向实际 Agent、收窄后服务器侧工具不受牵连、`client_files` 走同一门禁 |
| **远程委派（本轮新增）** | `tests/test_desktop_remote_execution_acceptance.py::test_a10_a_non_interactive_run_is_not_delegated_to_the_device` | 带 `local_execution_refusal` 的运行**不再**被委派到设备，队列新增 0 条 |
| | `::test_a10_an_ineligible_agent_is_refused_on_the_device_too` | 无项目工具资格的 Agent 同样在委派前被拒 |
| | `::test_a10_a_server_side_tool_is_not_collateral_damage` | `recall_memory` 等服务器侧工具不受牵连（它本来就不是委派调用） |
| 工具门禁不绕过 | `agent_stream` 的两道闸门：执行前的 `_agent_tool_allowed`（黑名单）与 `_permission_denial`；远程代理是**每轮现构**，不替换 `agent.tools[name]` | `test_desktop_remote_dispatch.py::RemoteDispatchTests::test_the_proxy_is_built_per_run_and_never_adopted_by_the_agent` |

### 本轮修掉的一处真实缺陷（A10，远程模式）

`execution_target_scope` 对所有桌面目标（本地与远程）都会把本轮拒绝写进
`identity.local_execution_refusal`（任务 3.7），本地路径在 `run_local_cwd` 里据此拒绝，
但**远程路径没有读它**：`dispatch.plan` 只问 grant 与 mode，于是「定时任务/后台唤醒」
或「无资格的实际执行 Agent」在本机项目不可达时，仍然会被委派到用户的设备上执行 ——
正是 A10 禁止的自动授权。

**修复**：`agent/desktop_remote/dispatch.py::plan` 在设备检查之前先读本轮拒绝，命中则
返回 `Plan(refusal=..., kind="unavailable", code="permission_denied")`，与本地路径的
「本轮拒绝优先」口径对齐。

**验证（先红后绿）**：新增上面三条 A10 用例，修复前 2 项失败（`planned.tool` 是代理工具），
修复后 3 项全过；回归 `tests/test_desktop_remote_dispatch.py` 92 项全过。

---

## A28 未批准、额度耗尽、磁盘不足、审计失败、许可过期

要求：按现有治理策略失败；预留能释放；无「成功空结果」；新读写/执行能力不绕过治理切片。

| 子项 | 用例 | 断言要点 |
| --- | --- | --- |
| 未批准动作 | `test_desktop_execution_broker.py::test_prepare_refuses_a_declared_action_without_an_approval`、`::test_prepare_accepts_the_approved_action_and_refuses_the_revoked_one` | `approval_required` 403；批准后 `checks.approval == "verified"` |
| 并发额度耗尽（prepare 时） | `::test_prepare_refuses_when_the_tool_call_quota_is_exhausted` | 硬限额用尽即 `limit_exceeded` 413（用例名沿用「quota_exhausted」的说法，线上码以 `limit_exceeded` 为准） |
| 并发额度耗尽（prepare 后、start 前） | `::test_start_refuses_a_quota_exhausted_after_prepare` | 两处独立重验，start 仍被拒（同上，`limit_exceeded` 413） |
| 许可过期 | `::test_a_permit_that_expired_cannot_start_anything`、`test_desktop_execution_commands.py::test_an_expired_permit_is_refused` | 见 A09 |
| 磁盘不足（设备侧） | **新增** `tests/test_desktop_remote_execution_acceptance.py::test_a28_a_device_out_of_disk_is_reported_never_as_a_success` | `resource_unavailable` 错误码、结果为 `error`、行状态 `failed`（非 `succeeded`） |
| 审计写入失败 | **新增** `test_desktop_execution_commands.py::test_an_audit_write_failure_does_not_silently_start_the_command` | 审记抛错 → 整个 start 事务回滚：状态仍 `acknowledged`、`started_at` 空、许可 `used_at` 空 |
| 预留能释放 | `test_desktop_remote_dispatch.py::BackgroundHandleTests::test_an_offline_device_gives_the_charge_back`、**新增** `::test_a28_a_quota_refusal_releases_the_reservation_and_runs_nothing`、`::test_a_released_charge_fits_under_the_limit_again` | 设备从没看到的调用退费（同一条 `quota_usage`），退费后同样的限额又能放进 5 次 |
| 「成功空结果」 | 全部失败路径都以 `status=error` 返回 `result` 文本（`RESULT` 断言 `assertNotIn("成功", ...)`）；`complete_execution` 拒绝与阶段矛盾的 effects | 见 `test_a17_*`、`test_a28_*` 与 `test_desktop_execution_commands.py::test_an_effect_claim_that_contradicts_the_phase_is_refused` |
| 不绕过治理切片 | `test_desktop_execution_broker.py::test_the_broker_refuses_while_the_capability_is_not_accepted`、`::test_the_declaration_is_what_closes_it`；`tests/test_desktop_execution_v2_contract.py::test_the_switch_alone_cannot_open_the_capability` | 切片 `implemented=True, accepted=False` + 默认关闭开关，打开开关也不使能能力 |

### 变异验证（先制造错误、确认失败、再还原）

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `_record_start` 在写审记**之前**就 `con.commit()`（状态落库、审记失败不回滚） | 「审计失败→回滚」应失败 | `test_an_audit_write_failure_does_not_silently_start_the_command` **1 failed**；还原后 23/23 |
| `plan` 去掉 `local_execution_refusal` 分支 | A10 远程拒绝退化为放行 | 2 项 A10 用例失败；还原后 92/92（dispatch）+ 63/63（acceptance） |

---

## 未覆盖 / 明确不做

- **Membership 行删除、Agent.use 撤销的专门用例**（见 A09）。
- **真实模型的端到端**：本文件的「模型」是注入到 `AgentStreamExecutor._execute_tool`
  的工具调用，不是字面 LLM；工具表、门禁、委派缝与结果投影是产品自身的。
- **真实网关 + 真机设备**：设备帧由产品自己的测试助手（`device_replies`）产生，服务端
  交换未改造；**安装包内**的等价验收属于第 10 组，未做。
- **磁盘不足的真实内核 ENOSPC**：本文件覆盖的是「设备如实上报 `resource_unavailable`
  时服务端不谎报成功」，不是在一个写满的卷上真跑一次；后者属于安装包/真机验收。
