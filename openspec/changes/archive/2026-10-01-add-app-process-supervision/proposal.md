## Why

2026-09-21 09:33–09:36 后端进程静默消失：`run.log` 停在正常的 `[WecomBot] ✅ Subscribe success`，stdout/stderr 无 ERROR、无 Traceback、无退出码线索；直到 09:36:03 守护任务才在 `watchdog.log` 记下 `app is down (nothing listening on 9900, no app.py process) -> starting`。这段时间里 nginx（443 → `127.0.0.1:9900`）拿不到上游，浏览器对 `/message` 的请求直接失败，重试耗尽后渲染前端通用文案「发送失败，请稍后再试。」（`channel/web/static/js/console.js` 的 `error_send`），用户看到的只是「发送失败」。

三处让这次死亡无法归因，且都会再次发生：

1. **进程不记录自己的结束方式。** Python 层的 `try/except` 看不到原生崩溃，`os._exit` 又绕过一切清理钩子 —— 于是「自身崩溃」与「被外部终止」在现有产物里完全同形。
2. **唯一可能留有线索的文件正是会被覆盖的那份。** `run-console.log` 在守护重启前被轮转为 `run-console.prev.log`，只保留一代，且下一次重启继续覆盖；被覆盖的恰是上一轮的 stdout 尾部。
3. **守护间隔 1 分钟。** 每次死亡至少有 60 秒是用户可见的失败窗口，09:35 那次正落在这个窗口内。

本 change 让死亡本身可诊断，并把重启窗口压到 ~20 秒。

## What Changes

- **新增 `common/process_watch.py`**：把进程生命周期写入与 `run.log` 同数据根的独立文件 `app-lifecycle.log`（`common/log.py` 新增 `log_path(filename)` 承接数据根规则）。四类记录构成闭集 —— 启动行、未捕获异常 traceback（主线程与工作线程）、原生致命故障的线程栈转储、退出行（`atexit` 的干净退出或 `os._exit` 路径的显式原因）—— 因此**有启动行而无任何退出行**唯一地指向「被进程外终止」。观测失败开放：记录不可写时退化到 stderr，不抛异常、不阻塞、不改退出码；记录超过 1MB 轮转一代。
- **`app.py`**：`run()` 首行（配置加载之前）安装记录，使启动期死亡与运行期死亡同样可诊断；三处 `os._exit` 路径（桌面 Web 渠道启动失败、Web 控制台超时、桌面启动失败）在退出前显式记录原因。
- **`channel/terminal/terminal_channel.py`**：`--cmd` 交互退出（`os._exit(0)`）同样显式记录原因。
- **`C:\rdai\scripts\watchdog-app.ps1`**（宿主本机脚本，不在仓库内）：存活探测由 `Get-NetTCPConnection`（CIM 查询）改为单次 TCP 连接，使亚分钟级轮询的代价可接受；发现后端不在时先读 `app-lifecycle.log` 尾部并写入 `watchdog.log`，无记录时明确记录这一缺失。
- **计划任务 `RDAI-AppWatchdog`**：由 1 分钟单触发器改为 3 个错开 20 秒的触发器（单条 `RepetitionInterval` 不允许低于 `PT1M`，注册报 `0x80041318`；错开触发器在同一约束内得到 20 秒节奏），action / `SYSTEM` / `IgnoreNew` / 5 分钟上限原样保留。

## Capabilities

### New Capabilities

- `app-process-supervision`：进程生命周期记录与死亡归因、观测失败开放、守护重启窗口与死亡线索。

### Modified Capabilities

<!-- 无：既有 capability（含 execution-isolation、database-runtime-consumers、platform-config-console）均不承载「进程监督与运维可观测性」责任域，按 openspec/config.yaml 的 rules 在本 change 内新建该 capability。 -->

## Impact

- **行为受影响**：后端额外写一个 `app-lifecycle.log`（`*.log` 已被 `.gitignore` 覆盖，不污染工作区）；四处 `os._exit` / 终端退出路径各多一行记录；守护重启窗口由 ≤60s 降到 ≤20s + 启动耗时。
- **不受影响**：请求与鉴权路径、渠道行为、`run.log` 与 `run-console.log` 的既有轮转方式、退出码语义（桌面模式仍退 1，`start-all.ps1` / `deploy.ps1` 的健康自检不变）。
- **代码面**：`common/log.py`（新增 `log_path()`，`_log_path()` 委托）、`common/process_watch.py`（新）、`app.py`（安装 + 三处退出记录）、`channel/terminal/terminal_channel.py`（一处）。
- **测试面**：新增 `tests/test_process_watch.py`（11 项，全部在子解释器里跑真实进程）。
- **文档面**：`启动说明.md` §4.3 / §4.4（**未跟踪**，gitignored）记录任务注册命令、20 秒节奏的来由与生命周期记录的读法。
- **未覆盖（登记为残留）**：宿主计划任务与 `C:\rdai\scripts` 均不在仓库内，本 change 只把它们写进口径文档，不自动改任务；2026-09-21 那次 PID 7128 的**具体**死因仍未确定 —— 本 change 的验收标准是「下一次死亡能被归因」，不是「解释这一次」。
