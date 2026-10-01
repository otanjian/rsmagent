# Tasks: 让后端死亡可归因，并把守护重启窗口压到 20 秒

> 依据：`openspec/specs/` 无「进程监督 / 运维可观测性」责任域，本 change 新建 capability
> `app-process-supervision`。需求侧 delta 见 `specs/app-process-supervision/spec.md`。

## 1. 进程生命周期记录（实现）

- [x] 1.1 `common/log.py`：抽出 `log_path(filename)`，`_log_path()` 委托之，使 `run.log` 与生命周期记录共用同一条数据根规则（`COW_DATA_DIR` 优先，否则 CWD）
- [x] 1.2 新增 `common/process_watch.py`：`install()` 写启动行（pid/argv/解释器/工作目录）、`faulthandler.enable(file=..., all_threads=True)` 转储原生致命故障、`sys.excepthook` 与 `threading.excepthook` 记录未捕获异常、`atexit` 写干净退出行
- [x] 1.3 `record_exit(reason)`：供绕过 `atexit` 的 `os._exit` 路径在退出前显式记录原因，使「无退出行」唯一指向进程外终止
- [x] 1.4 失败开放：记录不可创建/不可写时退化到 stderr，不抛异常、不阻塞、不改退出码；记录超过 1MB 轮转一代
- [x] 1.5 `app.py`：`run()` 首行（配置加载之前）安装；三处 `os._exit`（桌面 Web 渠道启动失败、Web 控制台超时、桌面启动失败）前记录原因
- [x] 1.6 `channel/terminal/terminal_channel.py`：`_shutdown()` 的 `os._exit(0)` 前记录原因

## 2. 守护重启窗口与死亡线索（实现）

- [x] 2.1 `C:\rdai\scripts\watchdog-app.ps1`：存活探测改为单次 TCP 连接 —— `Get-NetTCPConnection` 的模块加载 + CIM 查询在 20 秒节奏下的代价高于它保护的服务本身
- [x] 2.2 同上：发现后端不在时，先把 `app-lifecycle.log` 的尾部 12 行写进 `watchdog.log`，再轮转控制台日志、再拉起；无记录时明确记录「上一进程没有留下结束方式」
- [x] 2.3 计划任务 `RDAI-AppWatchdog`：3 个触发器，`RepetitionInterval=PT1M`、启动边界错开 20 秒（00:00:00 / 00:00:20 / 00:00:40）；action、`SYSTEM`、`IgnoreNew`、`ExecutionTimeLimit=PT5M`、`StartWhenAvailable` 原样保留（改后 XML 逐项复核）
- [x] 2.4 脚本健康路径实跑：退出码 0，未写任何日志（幂等静默）

## 3. 测试

- [x] 3.1 新增 `tests/test_process_watch.py` **11 passed**，全部在子解释器里跑真实进程：启动+干净退出、被 `kill` 后无退出行、原生故障转储（`Windows fatal exception: access violation`）、`os._exit` 显式原因、主线程异常 traceback、工作线程异常被记录且进程不退出、不可写降级到 stderr 且照常启动、重复安装只记一次启动、超限轮转出 `.1`、路径随 `COW_DATA_DIR`、记录不是被轮转的那份控制台日志
- [x] 3.2 既有套件回归（`app.py` / 渠道启动相关）：`test_channel_startup_open / test_startup_hook_seam / test_channel_double_start / test_scheduler_channel_resolution / test_external_store_version_guard / test_migration_recovery_acceptance / test_channel_instances / test_channel_signature_seam / test_channel_type_admissibility / test_tenant_channel_startup_synthesis / test_tenant_channel_hot_restart / test_web_channel_disconnect / test_tenant_channel_instances_service` **140 passed, 1 skipped**；`test_memory_global_config.py` 的 6 项失败经 `git stash` 对照确认为**既存失败**（改动前后失败集合逐一相同，根因是与本 change 无关的本机 workspace 路径）
- [x] 3.3 守护节奏实测（沙箱任务，同构造 + SYSTEM）：180 秒内 10 次触发，间隔 20.0 秒（09:53:01/21/41、09:54:01/…/09:56:01）
- [x] 3.4 真实任务节奏实测：`Microsoft-Windows-TaskScheduler/Operational` 事件 100 计数，`UseUnifiedSchedulingEngine=true` 下错开触发器同样按 20 秒触发 —— 09:57:21 / :41、09:58:01 / :21 / :41、09:59:01 / :21 / :41（8 次 / 140 秒）。测完已把该操作日志改回关闭；`Get-ScheduledTaskInfo` 的 `NextRunTime` 对多触发器任务不可信，不作为证据

## 4. 运行面观测（真实进程）

- [ ] 4.1 重启后端，确认 `C:\rdai\rsmagent\app-lifecycle.log` 出现启动行（pid/argv/解释器/工作目录）
- [ ] 4.2 停后端（`stop-all.ps1 -Service backend`）后由守护自动拉起：≤25 秒恢复，且 `watchdog.log` 含上一进程生命周期记录尾部
- [ ] 4.3 人为终止新进程（`taskkill /F`）：记录含启动行、不含退出行；`watchdog.log` 的尾部能直接读出「进程外终止」而非「自身退出」
- [ ] 4.4 探活恢复：后端直连 `/chat`、经 nginx `/chat` 均 200

## 5. 文档与收口

- [x] 5.1 `启动说明.md` §4.3 / §4.4：补 `RDAI-AppWatchdog` 的注册命令、20 秒节奏的来由（`PT1M` 下限 + 错开触发器 + `Get-ScheduledTaskInfo` 不可信）、`app-lifecycle.log` 的读法（四类行的组合判读表）
- [x] 5.2 本地提交（`rdai` 分支，只含本 change 文件；`deploy.ps1` 要求被跟踪文件无未提交改动，见 `启动说明.md` §3.1；`*.log` 与 `启动说明.md` 均不在提交范围）。**未 push**：按用户 2026-09-21 的选择「只本地提交」
- [x] 5.3 遗留登记：2026-09-21 那次 PID 7128 的具体死因仍无法确定；本 change 的验收标准是「下一次死亡能被归因」
- [ ] 5.4 需人工选时机的上线验证（见 `evidence/1-verification.md` §4）：重启后端使新代码生效，并完成 4.1–4.4
