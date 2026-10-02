# 验证证据：进程生命周期记录与守护重启窗口

日期：2026-09-21（Windows Server，`C:\rdai`，Python 3.12.10）

## 1. 生命周期记录（`tests/test_process_watch.py`，11 passed）

全部在**子解释器**里跑真实进程，不向测试进程注入 `faulthandler` / `atexit`。

| 用例 | 断言的事实 |
| --- | --- |
| `test_a_start_and_a_clean_exit_are_both_recorded` | 启动行含 `python=`，且 pid 记为子进程自己的 |
| `test_a_killed_process_leaves_no_exit_line` | 子进程驻留后被 `kill` ⇒ 记录含启动行、**不含任何退出行**（本能力的核心判据） |
| `test_a_fatal_native_fault_dumps_the_stack` | `ctypes.string_at(0)` ⇒ 记录出现 `Windows fatal exception: access violation` 与线程栈 |
| `test_an_os_exit_path_announces_itself` | `record_exit(...)` + `os._exit(1)` ⇒ 记录含 `exiting reason=...`、不含 `atexit` 行 |
| `test_an_unhandled_exception_records_its_traceback` | 含 `RuntimeError: startup exploded` |
| `test_a_worker_thread_failure_is_recorded_without_killing_the_process` | 工作线程异常被记录，进程仍正常退出（不致命） |
| `test_an_unwritable_record_degrades_to_stderr_and_still_starts` | 父目录不可写 ⇒ 启动行出现在 stderr、进程退出码 0、不建文件 |
| `test_installing_twice_records_one_start` | 幂等 |
| `test_the_record_is_rotated_instead_of_growing_without_bound` | 超 1MB ⇒ 轮转出 `.1` |
| `test_the_record_sits_beside_the_main_log` | 路径随 `COW_DATA_DIR`，覆盖变量优先 |
| `test_the_record_is_not_the_log_the_launcher_rotates` | 记录不是会被覆盖的 `run-console.log` |

```
tests/test_process_watch.py ...........  [100%]
11 passed in 1.59s
```

## 2. 守护节奏

### 2.1 为什么不是一条 20 秒的触发器

```
Set-ScheduledTask ... -RepetitionInterval (New-TimeSpan -Seconds 20)
⇒ Set-ScheduledTask : 任务的 XML 包含格式不正确或超出范围的值。
   (9,27):Interval:PT20S        HRESULT 0x80041318
```

单条 `RepetitionInterval` 的下限是 `PT1M`（`UseUnifiedSchedulingEngine` 开或关都拒绝）。
改用**错开触发器**：3 个触发器各 `PT1M`、启动边界相隔 20 秒。

### 2.2 沙箱任务实测（同构造 + SYSTEM）

任务 `RDAI-SubMinuteProbe`，动作只追加一行时间戳；测完已删除。

```
09:53:01.868  09:53:21.747  09:53:41.695  09:54:01.721  09:54:21.720
09:54:41.702  09:55:01.722  09:55:21.705  09:55:41.681  09:56:01.701
count=10   （180 秒 / 10 次 = 20.0 秒一次）
```

### 2.3 真实任务 `RDAI-AppWatchdog` 实测

证据来源：临时打开任务计划程序操作日志，按**事件 100**（任务实例启动）过滤该任务，测完已关闭该日志。

```
方法：wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
      ... Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'; Id=100}
      ... Where-Object { $_.Message -match 'RDAI-AppWatchdog' }

09:57:21.269  09:57:41.268
09:58:01.269  09:58:21.326  09:58:41.271
09:59:01.272  09:59:21.325  09:59:41.271
runs=8        （140 秒 / 8 次 = 20 秒一次）
```

改后任务 XML 逐项复核（`schtasks /query /tn RDAI-AppWatchdog /xml`）：3 × `TimeTrigger` /
`Interval=PT1M` / 边界 `00:00:00`、`00:00:20`、`00:00:40`；`UserId=S-1-5-18`、
`RunLevel=HighestAvailable`、`MultipleInstancesPolicy=IgnoreNew`、`ExecutionTimeLimit=PT5M`、
`StartWhenAvailable=true`、`UseUnifiedSchedulingEngine=true` 均原样保留。

### 2.4 一个坑（写进 `启动说明.md` §4.3）

多触发器任务的 `Get-ScheduledTaskInfo` 的 `NextRunTime` / `LastRunTime` **不可信**（实测给出
不属于 20 秒网格的时刻），不能用它判断守护是否在工作；守护健康时又故意不写 `watchdog.log`，
因此判断真实节奏只能靠操作日志事件 100。

## 3. 回归

```powershell
python -m pytest tests/test_channel_startup_open.py tests/test_startup_hook_seam.py `
  tests/test_channel_double_start.py tests/test_scheduler_channel_resolution.py `
  tests/test_external_store_version_guard.py tests/test_migration_recovery_acceptance.py `
  tests/test_channel_instances.py tests/test_channel_signature_seam.py `
  tests/test_channel_type_admissibility.py tests/test_tenant_channel_startup_synthesis.py `
  tests/test_tenant_channel_hot_restart.py tests/test_web_channel_disconnect.py `
  tests/test_tenant_channel_instances_service.py tests/test_process_watch.py -q
⇒ 140 passed, 1 skipped in 260.37s
```

`tests/test_memory_global_config.py` 的 6 项失败：用
`git stash push -- app.py common/log.py channel/terminal/terminal_channel.py` 取基线后**同样失败 6 项、
集合逐一相同**（根因是本机 `agent_workspace` 路径，与本次改动无关）。

## 4. 尚未验证（需重启后端，人工选时机）

- [ ] `C:\rdai\rsmagent\app-lifecycle.log` 出现启动行
- [ ] 停后端后由守护在 ≤25 秒内拉起，`watchdog.log` 含上一进程记录尾部
- [ ] `taskkill /F` 后记录只有启动行、没有退出行，守护记录可直接读出「进程外终止」
- [ ] 后端直连与经 nginx 的 `/chat` 恢复 200

> 验收口径：本能力的标准是「**下一次**死亡能被归因」；2026-09-21 09:33–09:36 那次
> PID 7128 的具体死因仍无法确定（旧代码没有留下任何结束方式的记录）。
