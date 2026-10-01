# 故障注入证据（A14—A20）

对应任务 7.7（以及 7.1—7.6 的验收面）。本文覆盖**副作用命令在故障下的行为**：
回执丢失、开始窗口崩溃、重复/篡改命令、进程失联与并发。

原文日志：
- 用例运行：`journal-fault-mutations.log`（12 类走样注入 + 还原记录）
- 用例本体：`tests/test_desktop_execution_journal.cjs`（20）、
  `tests/test_desktop_command_runner.cjs`（31）、
  `tests/test_desktop_device_execution.cjs`（21）、
  `tests/test_desktop_execution_wiring.cjs`（2）

合跑：

```bash
node --test tests/test_desktop_execution_journal.cjs \
           tests/test_desktop_command_runner.cjs \
           tests/test_desktop_device_execution.cjs \
           tests/test_desktop_execution_wiring.cjs
# → 74 passed / 0 failed
```

## 真实的部分

| 环节 | 是否真实 |
|---|---|
| journal 落盘 | **真实**：真实文件系统上的真实 JSON 文件，`tmp + rename` 原子替换 |
| 崩溃 | **真实**：在**同一路径上新建第二个 `JournalStore`** —— 这正是重启后的进程所做的事，不是 mock 的 `recover()` 调用 |
| 去重/冲突/过期 | **真实**：编译后的 `JournalStore` / `CommandRunner`，非替身 |
| 进程终止、串行、取消 | **真实**：真实子进程句柄与真实 `terminate` 回调 |
| 收敛/显示 | **真实**：`DeviceExecution.status()` 与 broker 的 `reconcile` 字段 |

被注入的只有故障本身（下面「走样」一节），业务判定全部来自产品代码。

## 逐条验收

| 项 | 注入的故障 | 观察到的行为 | 主要用例 |
|---|---|---|---|
| **A14** 回执前断网并重复投递 | 同一 `command_id` 第二次投递 | 返回**已存**判定，不重跑；与第一次结果逐字节相同；不追加第二条成功消息 | `A14: a redelivered frame returns the stored verdict and does not run again`、`A14: a redelivery before the receipt is durable returns the stored result and does not re-run`、`A14: a second delivery while the first is still running is not started alongside it`、`a start intent reaches disk before the effect can run` |
| **A15** 开始意图落盘后强杀 | 在 start 与 receipt 之间换进程 | 判定为 `outcome_unknown`，**绝不自动重跑**；`effects: unknown`，不得上报为成功 | `A15: a crash after the start intent leaves outcome_unknown, never a re-run`、`… never a silent re-run`、`a crash between start and receipt becomes outcome_unknown, and is not retried` |
| **A16** 同 id 改参数/摘要；清理后重放 | 同 id 不同 `params_digest`；回执被清理后再投递；超过 `deadline` | 一律**按名拒绝**（`command_conflict` / 过期），第一个载荷保持不变；清理留下**墓碑**，已花掉的命令不会重新变"新鲜" | `A16: the same command id with different arguments is refused by name`、`… and the first payload is untouched`、`A16: a frame past its deadline is expired, not started late`、`A16: a command whose receipt was cleaned up cannot become executable again` |
| **A18** 取消 / 登出 / 切租户，且已写部分文件 | 取消在途与排队中的运行；登出；切租户 | 取消**等进程真正结束后**才返回，已写文件保留并如实提示部分效果；迟到结果**不能覆盖**取消结论；登出停本会话，切租户只停离开的那个会话 | `A18: cancel resolves only after the tree ended, and the reply says so`、`A18: a late result from a cancelled run cannot overwrite the cancellation`、`A18: cancelling a queued command writes effects none and never runs it`、`A18: a sign-out stops this session`、`A18: a tenant switch stops only the session that is going away` |
| **A19** 短断线与长断线 | 断开后恢复；判据取边界值与两侧 | 短于存活期**不终止**任何运行（含**恰好等于**边界）；超过存活期才终止，且保留已写内容；断线期间新帧不受理，也不转投服务器 | `A19: an outage inside the liveness window terminates nothing`、`A19: the exact liveness boundary is still inside the window`、`A19: past the liveness window the run is stopped and what it wrote is kept`、`A19: while the connection is down no new frame is accepted` |
| **A20** 同根多 alias 并发写 + 外部修改 | 两个 alias 指向同一实际根；外部编辑器改/删文件 | 同根有副作用调用**串行**（不同根不互相阻塞）；普通 edit 检出外部修改 → `file_changed` 并标注部分效果；删除报 deleted；不承诺 Bash 事务回滚 | `A20: two aliases of one real root serialise effectful commands`、`A20: an outside edit is reported as file_changed with partial effects`、`A20: a file deleted after the read is reported as deleted`、`A20: a first write to a file never read is not stale` |
| **A17** 脚本退出/超限/后台子进程 | — | 见下「不在本文范围」 | `tests/test_desktop_local_execution.cjs`、`evidence/platform-probes.md` |

## 用例真的能发现实现走样（12 类注入）

只跑绿不能证明用例有判别力，所以 `evidence/scripts/mutate_desktop_journal.py`
把实现改成一条"看起来更省事"的写法，要求对应用例**必须失败**，随后立刻还原：

| 注入 | 走样内容 | 命中用例 |
|---|---|---|
| M1 | 开始意图不落盘，直接执行 | `a start intent reaches disk before the effect can run` |
| M2 | 去重键丢掉租户与设备 | `the dedup key is the contract's own field list` |
| M3 | 崩溃恢复把未完成当作没跑过 | `A15: … never a silent re-run` |
| M4 | 同 id 不同摘要不判冲突，按新载荷执行 | `A16: … the first payload is untouched` |
| M5 | 清理回执不留墓碑 | `A16: a command whose receipt was cleaned up cannot become executable again` |
| M6 | 有副作用命令不再独占项目根 | `A20: two aliases of one real root serialise effectful commands` |
| M7 | 外部修改/删除一律不报 | `A20: an edit reports an outside change…`、`… deleted…` |
| M8 | 存活期判据反转 | `A19: an outage inside the liveness window terminates nothing` |
| M9 | 取消不等进程真正结束 | `A18: … keeps the files already written` |
| M10 | 结果帧把 `outcome_unknown` 报成成功 | `A15: … never a re-run` |
| M11 | 帧里的设备标识不核对 | `a frame naming another device is refused without touching the journal` |
| M12 | 保留期清理**实现了但没人调用** | `7.6: reaching the execution endpoint reclaims receipts past the retention window` |

12/12 全部被对应用例判为失败，且还原后源文件逐字节一致（脚本退出码 0）。

**M12 是本次新增，且抓到过真问题。** 保留期清理（7.6）此前是完整实现、
完整单测、但**没有任何调用方**：真实安装会无界增长
`execution-journal.json`，而所有套件全绿。单元测试无法发现"没人调用"，
只有像 `tests/test_desktop_execution_wiring.cjs` 这样从**应用入口**走进去的
用例才能发现。修法是让 `LocalReadAssembly` 在首次构建执行端点时与 recovery
共用同一个"上电"缝隙 `endpoint.sweep()`（保留期为契约自身的
`journal_retain_days`；契约未定义条数上限，因此不在此处臆造数字）。

## 边界与不在本文范围

- **A17 的进程级部分**（非零退出、超时、大量 stdout、资源超限、后台子进程）
  由 4.4/4.9 的平台证据承担：`tests/test_desktop_local_execution.cjs` 与
  `evidence/platform-probes.md`。本文只覆盖"失败也必须留下回执"这一交付面。
- **Windows** 的同等探针仍未完成（任务 4.5/4.9），因此本文结论仅对 macOS 成立。
- journal 的**条数上限**没有在客户端设定：契约只定义 `journal_retain_days`，
  `prune` 的 `keepAtMost` 保留为调用方显式传入的后备手段。若后续要求硬上限，
  应先落到 `contracts/desktop/v2.json` 再接线，而不是在客户端写死。
