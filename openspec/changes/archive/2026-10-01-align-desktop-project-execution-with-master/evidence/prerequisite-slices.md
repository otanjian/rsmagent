# 所消费治理切片的逐项登记（任务 1.6）

`integration-map.md` §2 列出本 change 消费的九个既有 capability。这份表把其中**真正被
v2 执行路径调用**的六个（身份、授权、审批、审计、凭据、配额）逐项登记「已具备证据」与
「待补证据」，并按任务 1.6 的要求判定**它到底阻塞哪些任务**——只阻塞确实依赖该缺项的
那一部分，不把"以后还要做真机验收"笼统地放大成"整个 change 不能推进"。

本轮实跑（`.venv/bin/python -m pytest ... -q -p no:randomly`）：

```
tests/test_desktop_auth_flow.py            37
tests/test_desktop_identities.py            6
tests/test_desktop_local_worker.py         47
tests/test_desktop_execution_broker.py     46
tests/test_desktop_execution_commands.py   27
tests/test_action_approval_consumer.py     26
tests/test_desktop_run_authorization.py    37
                                    ─────────
                                     226 项（224 passed / 2 failed → 见 §7，已修）
```

> 说明：前两行是**身份**切片、`local_worker` 是**凭据**切片、`broker`/`commands` 是
> **授权+审批+审计+配额**四条切片的真实消费点，`action_approval_consumer` 是审批切片的
> 生产接缝，`run_authorization` 是资格/授权收窄。它们合起来是本表的证据底座。

---

## 1. `desktop-tenant-context`（身份与配对）

**本 change 的消费点**

- 原生 Bearer 只保存在主进程内存；broker 的四条端点要求 **native bearer**，
  Cookie 页面会话在授权判断**之前**即 401（`execution_broker.py` 模块注释 27–29 行）。
- 每条命令都绑定 owner / 设备 / binding / 项目 / `grant_version` / 调用摘要，
  跨作用域轮询被拒。
- 设备登记在当前**有效原生会话**的真实用户下；设备 ID 与自报平台只作定位，不能独立认证。

**已具备证据**：`tests/test_desktop_auth_flow.py`（37）、`tests/test_desktop_identities.py`（6）、
`tests/test_desktop_execution_broker.py`（四条端点的真实 app + 真实库）、
`evidence/run-context-and-revocation.md`、`evidence/remote-workbench-loopback-fix.md`。

**待补证据**：真实安装包内、两个用户两个租户的切换与撤权（属任务 10.1–10.3）。

**阻塞判定**：只阻塞 A04/A33 的**真机编号**验收与 10.2/10.3；**不阻塞**本机模式的实现与
开发机侧验收——P1/P2 门槛（5.5/7.8）已按开发机证据通过。

---

## 2. `resource-execution-authorization`（逐次执行授权）

**本 change 的消费点**：`prepare` 与 `start` **两处独立重验**授权；`start` 是唯一签发点；
执行目标把授权**收窄**到具体项目根；只读 grant / 只读会话 / 未开放脚本能力的设备
**不会**因为"选了项目"而升级权限。

**已具备证据**：`tests/test_desktop_execution_broker.py`（撤权、配额、审批在 prepare 与
start 各自独立重验）、`tests/test_desktop_run_authorization.py`（37，资格收窄）、
`evidence/target-resolution-acceptance.md`、`evidence/run-context-and-revocation.md`；
变异 R10（去掉注册表 `grant_version` 比对）已被用例判失败。

**待补证据**：无阻塞性缺口。

**阻塞判定**：不阻塞任何任务。

---

## 3. `action-approval`（单动作审批）

**本 change 的消费点**：需要审批的工具动作，命令必须带 `approval_id`；broker 在
prepare 与 start **重新读 `approvals` 行**，校验它仍存在、`status` 仍有效，
并绑定实际命令/摘要——"存在一个审批"本身不构成放行依据（`_check_approval` 的注释明确写了
这条）。工具侧的适用性判断复用 `agent.approval_gate.is_required` / `tool_action_id`。

**已具备证据**：`test_desktop_execution_broker.py::test_prepare_refuses_a_declared_action_without_an_approval`、
`::test_prepare_accepts_the_approved_action_and_refuses_the_revoked_one`、
`tests/test_action_approval_consumer.py`（26，含生产接缝 `AgentStreamExecutor._execute_tool` 的
"有审批才跑、审批用一次即失效、重放不生效"）、`evidence/governance.md` A28。

**待补证据**：由**真实审批控制台**产生审批、再驱动 v2 命令的端到端（属任务 10.2/10.3）。

**阻塞判定**：不阻塞实现——消费的是真实 `approvals` 表与真实 `approval_gate`，
不是占位；只在真机编号验收时补。

---

## 4. `audit-log`（审计）

**本 change 的消费点**：`integrations/desktop/commands.py` 六处 `_audit.record(...)`，
覆盖开始 / 终态 / `outcome_unknown` / 恢复；三条 v2 审计都带
`run_id` / `tool_call_id` / `journal_id` / `permit_id` / `connection_epoch` / `grant_version`，
且**不含**路径、令牌或环境变量。

**已具备证据**：`tests/test_desktop_execution_commands.py`（含
`test_the_lifecycle_audit_records_start_and_terminal`）、`evidence/governance.md` A28
（"审记写入失败 → 整个 start 事务回滚：状态仍 `acknowledged`、`started_at` 空、许可 `used_at` 空"），
对应变异（在写审记**之前**先 commit）已被判失败。

**待补证据**：无。

**阻塞判定**：不阻塞任何任务。

---

## 5. `credential-management`（凭据与环境边界）

**本 change 的消费点**：worker 的环境是**白名单 + 具名 deny pass**，
且**两端各洗一次**（启动器 `desktop/src/main/local-execution/env.ts` 与
`agent/desktop_local/worker.py::build_worker_env`），所以任意一端漏了都不会成为单点。
Bearer、模型密钥、会话 Cookie 与身份库路径都不进 worker；缺凭据/网络授权时**明确失败**，
不复制主进程或服务器环境。

**已具备证据**：`tests/test_desktop_local_worker.py::WorkerEnvTests::test_keeps_only_what_a_tool_needs`
（该文件 47 项）、A27 的"不继承环境秘密 / 身份库与主进程配置不可读 / 技能缓存只读"
（`evidence/isolation-acceptance.md`）、`evidence/platform-probes.md` §2.1 的三处必需许可。

**待补证据**：Windows 侧 `env.ts` 同一口径（`SYSTEMROOT` 等属 Windows 分支）未在真机验证。

**阻塞判定**：阻塞**Windows 平台结项**（与任务 4.5/4.9 同一原因），不阻塞 macOS；
因此第 10 组的平台矩阵不能勾选，但 macOS 侧结论不受影响。

---

## 6. `resource-quota`（硬配额与退费）

**本 change 的消费点**：`quota_available` 在 prepare 与 start **各查一次**；
预留与释放用的是**同一张** `quota_usage` 表（`IdentityService.refund_quota`），
不建第二套业务配额。

**已具备证据**：`evidence/governance.md` A28 的三处（prepare 时耗尽、prepare 后 start 前耗尽、
退费后可再放进 5 次），拒绝码实测为 **`limit_exceeded` 413**；
`test_the_broker_refuses_while_the_capability_is_not_accepted`。

**待补证据**：真实并发压测（多个会话同时抢同一额度）。

**阻塞判定**：不阻塞实现；并发压测属第 10 组加固项，不构成门槛。

---

## 7. 本轮发现并修掉的一处真实缺陷（任务 1.6 的意外收获）

跑上表前两组合集时，**两条期望成功的审批用例失败**：
`test_action_approval_consumer.py::test_the_same_action_runs_when_its_approval_matches`
与 `::test_an_undeclared_action_still_runs_with_a_recorded_basis` 都得到
`wrong status - error`，结果是
`'AgentStreamExecutor' object has no attribute 'on_event'`。

**根因**：产物卡片发射（任务 9.1）的守卫写 `if not self.on_event`，而它在
`_maybe_emit_remote_artifacts` 自身的 `except Exception` **之外**；该方法的调用点在
`_execute_tool` 的 `try` 内，其 `except Exception` 会把逸出的异常转成
`{"status": "error", "result": str(e)}`。于是**一个只该影响呈现的字段缺失，
把一轮真实成功的产物写入报成了工具失败**——模型读到的是那句 AttributeError，
而不是它自己的写入结果。

**修复**：`agent/protocol/agent_stream.py` 两处守卫改读 `getattr(self, "on_event", None)`；
本地那半拆成 `_maybe_emit_artifact`（守卫）+ `_publish_artifact`（整体包在
`except Exception` 内并 warning，与远端那半的既有口径一致）。

**先红后绿**：新增 `tests/test_desktop_artifact_source.py::EmissionIsolationTests`
（5 项：缺失接收器 ×2、校验器抛错、接收器抛错、本地写入仍出卡片）。修复前 2 项失败，
修复后 `test_desktop_artifact_source.py` + `test_action_approval_consumer.py`
共 **66 passed, 4 subtests**。完整记录见 `evidence/artifact-source.md` §六。

**为什么记在这里**：这正是任务 1.6 要核对的"切片是否真的被正确消费"——审批切片的
`_execute_tool` 接缝被**另一个能力的呈现逻辑**污染了。如果不做这次切片核对，
它会一直在真机上把成功的写入报成失败。

### 7.1 顺带纠正的一处口径错误

`evidence/governance.md` 原先写"硬限额用尽即 `quota_exhausted`"。实测线上码是
**`limit_exceeded`（413）**（`execution_broker.py::_check_quota` 抛出、用例断言）
——`quota_exhausted` 只是**用例名**里的说法，不是对外错误码。两份文档已同时更正，
`operations.md` 的错误码台账把两者显式区分，避免运维按错误码去搜日志。

---

## 8. 结论

- 六个切片**全部是真实消费**（真实表、真实门禁、真实审计行、真实环境剥离），
  没有一个是占位实现；
- 唯一的门槛性缺项是 **Windows 平台与安装包内验收**，它阻塞第 10 组的平台矩阵，
  **不阻塞** 1.x/5.x/7.x/8.x/9.x/11.x 已完成的实现与开发机验收；
- 本轮由这次核对直接修掉一处"呈现污染结果"的真实缺陷（§7），并纠正一处错误码口径（§7.1）。
