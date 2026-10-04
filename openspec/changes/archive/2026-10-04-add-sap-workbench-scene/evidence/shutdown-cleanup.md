# SAP 工作台进程退出与资源回收证据

本记录只覆盖场景局部的生命周期修复与隔离验证。没有停止当前 Web/桌面或用户浏览器，没有启动 Chrome、Bun、OpenCode、SAP/MCP 实际组件，没有调用模型或保存 SAP 数据。测试里的真实子进程均为临时 Python `sleep` 替身；子解释器仅启动临时 loopback 网关。没有修改 app/core、普通智能体、公共 ChromeLauncher 或全局信号处理器。

## 修复依据与实现

原场景网关使用 daemon 线程，但没有进程退出钩子。主程序既有 SIGTERM → `sys.exit` 路径不会自然执行该 daemon 的 async finally，可能留下场景启动的独立 Chrome/Bun。原 `stop()` 只 join 5 秒，串行 runtime 回收还可能因一个异常跳过后续组件。

`BrowserGatewayRunner.start()` 仅在网关已真实绑定成功后，为该实例注册一次 `atexit(_exit_stop)`。单纯 import、未使用、无效 `SAP_WORKBENCH_NODE_MAX_SESSIONS` 和启动失败都不会注册场景退出钩子。钩子校验注册时 PID，不覆盖任何 signal handler。重复 start/stop 使用同一退出注册；正常 WSGI/main 调用 stop 可复用。

退出先设置 runner 停止标记、manager closing 标记和所有既有 node 的线程安全停止 Event，再取消尚未完成的 allocation。网关同时启动 runtime、node、manager、HTTP 清理。每个组件的异常分别处理，不能中断其他组件。runtime 在外部组件等待前将会话置于 paused，恢复未决动作到 unknown；最终再次恢复。历史、配置和 remote session 标识保留，不重播动作，也不把取消认作 SAP 已回滚。

每个 runtime 保有被 shield 的主机 spawn Task；取消/清理早于 spawn 返回时，完成回调仍登记并关闭真实子进程。node 的 native launch、target URL 读取和 native close 改用首次运行时启动、每节点自持的持久 daemon worker、私有串行队列与 loop Future。未使用默认 ThreadPoolExecutor，因为其解释器退出 join 发生在普通 atexit 前，会使场景无法及时设置停止标记。Python 3.12+ 也禁止在 atexit 中新建线程，因此退出只向既有 worker 排队关闭。单节点顺序、节点之间并行，已关闭 worker 会退出，空闲活节点最多占一个 worker。node launch 在同一线程 finally 中检查 Event 并清理迟到 spawn，loop 已关闭时也不依赖重新提交回调。

场景 launcher 接缝在 `_proc` 首次赋值时记住确切 Popen，即使公共 launcher 在 close 中清空 `_proc`，仍能找回实际子进程。runner 对尚未证明结束的真实 handle 强持有，避免 node 弱引用或 runtime registry 删除后丢失。普通 node/runtime close 的快速异常路径也会对仍活的自有 handle kill/reap，并非只有总超时才兜底。应急回收只使用本 runner 记录的 Popen/asyncio subprocess 底层 Popen，不扫描进程、不推断 PID、不关闭普通 Chrome。

## 等待预算与边界

| 路径 | 源码预算 | 并发与边界 |
| --- | --- | --- |
| 网关正常清理 | 46 秒 | runtime、node、manager、HTTP 与 pending allocation 同时等待；不会逐个乘以租户/节点数 |
| 私有 asyncio loop 最后取消排空 | 2 秒 | 替代 `asyncio.run` 对拒绝取消任务的无界最终 gather |
| main/WSGI `runner.stop()` | 55 秒共享等待截止时间 | join 后 kill 只针对自有 handles；各 Popen reap 使用剩余共享时间，不为每个 child 再加 55 秒 |
| runtime | tool 取消等待 2 秒；组件并行等待 42 秒；超时取消等待 2 秒；child reap 共享 2 秒 | 正常子进程关闭更早结束；runner 上层仍有 46 秒 grace 与 55 秒 main 等待 |
| 主机 child | terminate 后 5 秒，kill 后 2 秒 | 主机 spawn 未返回另等 2 秒，迟到完成由自身回调关闭 |
| MCP child | 既有 close 12 秒，kill wait 2 秒 | 本轮没有改变 MCP SDK/worker 的生命周期策略 |
| native Chrome | 既有 readiness 25 秒、close wait 8 + kill wait 5 秒；本场景精确 handle 兜底 reap 2 秒 | 同一 node 的关闭 Task 幂等，各 node 并行；全局节点上限仍默认 4、可配置 1–32，pending/releasing 原预留语义保留 |

55 秒是本场景 stop 对线程 join/子进程 wait 的共享预算，加上非阻塞遍历与调度开销。不是整个应用解释器在任何 OS 状态下都必然 55 秒内退出的承诺。其他应用的退出钩子、其他用途的全局 executor、阻塞 OS/native I/O 和同步存储锁都不由本场景控制；主线程超时后仍会仅对已知 handle 做应急 kill。同步台账不可写时记录固定 ledger/unavailable，后续场景恢复继续将未决状态归为 unknown。

兼容保留既有专属 profile adoption。已采用的旧 Chrome 没有本次启动的 Popen，仍使用公共 launcher 的正常 close；应急兜底不把 `_adopted_pid` 变成 kill 目标。旧 profile adoption 的真实 Chrome 场景没有在本轮重测。SIGKILL、`os._exit`、系统掉电不执行正常 atexit，亦不属于这份正常退出证据。

本轮新增的异常/超时日志只包含固定 `component/code`，不拼接原始异常、环境变量、headers、凭据或 MCP 回执。

## 可重复的隔离验证

```bash
.venv/bin/python -B -m pytest -q tests/test_sap_workbench_shutdown.py tests/test_sap_workbench_resource_cleanup.py tests/test_sap_workbench_lifecycle.py tests/test_sap_workbench_manager.py tests/test_sap_workbench_mcp_runtime.py tests/test_sap_workbench_deadline.py
```

结果：**91 passed in 2.60s**，运行解释器为 Python **3.14.3**。新增 `test_sap_workbench_shutdown.py` 为 22 项；其余专项复验既有取消/lease/容量/MCP 与 deadline 行为。deadline 专项包含实际 MCP worker Python 3.10 解释器的纯内存 AnyIO taskgroup 清理，属于既有离线兼容证据，没有连接 MCP/SAP 服务。

新证据包含：

- import-only、未启动、无效部署设置均无场景退出注册；正常启动重复调用只注册一次。
- 实际临时 Python 主机/浏览器替身在正常解释器退出、隔离 SIGTERM → `sys.exit`、重复 stop、慢组件超时和单 runtime 快速异常后都退出。
- 未决动作最终为 unknown，会话为 paused，remote session 标识及历史保留；私人错误标记不进入输出。
- node 在 native launch 延迟期间关闭；迟到进程由同一 launch 线程关闭；owning loop 已关闭时也能回收。
- 单个与 8 个并发迟到 launch 的子解释器退出。默认 executor 被限制为 1 worker，全部场景 launch 仍同时进入自持 daemon 线程；没有在默认队列等待。夹具全局节点限额为 32、同租户上限为 8，另一个既有节点仍占正常额度。
- 正常解释器退出的 adopted-profile 契约替身：launcher 没有 `_proc`，其临时 child 也未进入 runner handle registry，只能经已存在的 native worker 正常 close 回收。此项在 Python 3.14.3 正常退出下通过，证明不依赖 atexit 新线程，也不依赖应急 PID 扫描；它不是实际 Chrome profile adoption 验收。
- 公共 launcher 提前清空 `_proc` 并快速失败后，持久句柄仍被回收；node 被 GC 后 runner 仍持有确切活 child。
- runtime 主机/MCP close 同时快速失败，两个真实临时 child 仍被 kill/reap；主机 spawn 晚于 runtime close 返回也会登记并退出。

真实临时 child 创建后立即追加到夹具私有 PID ledger，READY 前失败/超时的 parent finally 也只回收自己的确切替身；不扫描全机。慢关闭子解释器仅将本夹具的 grace/drain/wait 缩短为 0.3/0.1/4 秒以验证预算收敛，不声称等待实际 55 秒才算通过。

实际 SAP Web GUI/OpenCode 操作、真实 Chrome adoption、真实 SAP 回执/保存验收仍保留原暂停/未验证状态。本证据不勾选这些现场任务。
