# 阶段 1 证据：P0 日志稳定性（任务 1.1、1.2、1.3）

实施日期：2026-10-13（本机 Windows x64，`D:\AIPJ\rsmagent`，分支工作树）。
对应规范：`specs/desktop-runtime-diagnostics/spec.md`（桌面日志故障不得递归传播、桌面日志存储与导出有界）。

## 1. 改动

| 文件 | 改动 |
|---|---|
| `desktop/src/main/host-log.ts`（新） | 宿主日志：同步落盘、10 MiB×2 轮转、终端断管保护、崩溃直写（不经过 console）、失败重试上限；无 Electron 依赖 |
| `desktop/src/main/data-dir.ts`（新） | 数据目录唯一来源 `resolveDataDir()` + `COW_DATA_DIR`（宿主日志与后端 `run.log` 同目录） |
| `desktop/src/main/index.ts` | `initDesktopLogging()` 改为安装宿主日志（`captureConsole`/`captureCrashes`）；移除写 `run.log` 的 console 包装与经 console 上报的崩溃处理器 |
| `desktop/src/main/python-manager.ts` | 后端就绪前的 stdout/stderr 与其 `[SHELL]` 诊断改写宿主日志；删除自建 `run.log` 追加流（双写入者） |
| `desktop/src/renderer/src/i18n.ts` | 启动失败提示与日志页说明同时定位 `run.log` 与 `desktop-main.log`（中/英） |
| `docs/desktop-runtime-logs.md`（新） | 两文件归属、轮转策略、旧超大日志的人工处理步骤（先停写入者） |
| `tests/test_desktop_host_log.cjs`（新） | 16 项回归 |

复现根因（巡检报告 §4.1）：`console` 包装先写文件、再调原始终端输出，且终端写入无保护；`uncaughtException` 处理器又走 `console.error`，形成「EPIPE → 上报 → 再 EPIPE」的自喂循环，`~/.cow/run.log` 实测 771 MB → 1,136.8 MB（3.6 MB/s）。

## 2. 关键设计取舍与实测

- **同步写入**（`fs.writeSync`，描述符由本模块持有）：不建队列、不缓冲。曾按 `WriteStream` + 背压实现「落后时跳过并计数」，实测在连续 5000 条写入时流在约 16 KB 高水位后持续返回 `false`，**连崩溃记录一起被丢弃**；同步实现使「接受即落盘」，突发事件一条不丢，且文件/内存天然有界。
- **轮转竞争（实测）**：先关闭描述符再重命名；预先用独立探针确认 Windows 允许重命名仍被本进程打开的文件（libuv 以 `FILE_SHARE_DELETE` 打开）：
  `{"renameWhileOpen":"ok","writeAfterRename":"ok","rotated":"one\ntwo-after-rename\n","fresh":"three-new-file\n"}`
  异步 `WriteStream` 版本曾在连续写入时以 `ENOENT` 静默丢失整个代际（文件尚未由异步 open 创建），已由同步描述符消除。
- **失败降级有界**：不存在的目录、持续 `ENOSPC` 均在第 3 次失败后停止重试（`HOST_LOG_FAILURE_LIMIT`），不抛回调用方。
- **不触碰后端文件**：轮转只处理 `desktop-main.log*`；同一目录下的 `run.log`、`identity.db` 在 200 次轮转后逐字节不变（用例锁定）。

## 3. 验证命令与结果

| 命令 | 结果 |
|---|---|
| `node --test tests/test_desktop_host_log.cjs` | **16 项全部通过**（列表见下） |
| `node node_modules/typescript/bin/tsc --noEmit`（renderer） | 通过 |
| `node node_modules/typescript/bin/tsc --noEmit -p tsconfig.main.json` | 通过 |
| `node --test tests/test_desktop_local_execution.cjs` | 59 项：36 通过 / 0 失败 / 23 跳过（跳过项需打包后端 `COW_A26_BACKEND`） |
| `node --test tests/test_desktop_lifecycle.cjs` | 5 项全部通过 |

用例覆盖（对应用户报告的事故路径）：

1. 文件命名与默认上限（10 MiB / 2 备份）；2. 宿主日志与后端日志同目录（开发=检出、打包=`~/.cow`、`COW_DATA_DIR` 优先）；3. 记录含级别与时间戳；4. 调用返回即已落盘（无队列）；5. 循环对象/BigInt/不可字符串化值/Error 均不抛错且成行；6. 目录不可写时有界降级；7. 持续 `ENOSPC` 有界降级；8. 轮转保留 1 活文件 + 2 备份且不重复写；9. 5000 条突发有界且末条不丢；10. `backups=0` 时按上限截断；11. 重启追加不覆盖历史并重新测量大小；12–14. 子进程三场景：异步 `error` 事件只静音一次且不升级为异常、同步抛错的 console 只静音一次、**2000+500 条风暴 + 两次 `uncaughtException`：文件有界、崩溃记录恰好各一条、缓冲区不再自我喂食**；15. 目录不可写不创建文件；16. 轮转不触碰 `run.log`/`identity.db`。

## 3.1 打包版复验（2026-10-14，真实 NSIS 安装包）

在**已安装的打包版**上复验"两个日志分离且同目录"（此前证据只有源码检出运行）：

| 观测 | 结果 |
|---|---|
| 数据目录规则（`desktop/src/main/data-dir.ts`） | 显式 `COW_DATA_DIR` 优先 → 打包版 `~/.cow` → 源码运行用检出目录。**打包版 = `C:\Users\admin\.cow`** |
| `desktop-main.log` | 存在（81,981 B），内容含**打包路径**的栈（`…\Programs\cowagent-desktop\resources\app.asar\node_modules\electron-updater\…`）→ 确由打包版写入 |
| `run.log` | 存在（38,765 B，**打包运行 11:53:04 写入**），尾部 `[WebChannel] Local access: http://localhost:9876`、`Listening on 127.0.0.1 only (local access)` |
| 是否同目录 | **是**（都在 `~/.cow`）——正是 `data-dir.ts` "单一来源让宿主日志、后端 `run.log` 与配置三者一致"的设计意图 |
| 附带观测 | 打包版后端**默认只监听 127.0.0.1**，未沿用开发 `config.json` 的 `web_host=0.0.0.0` ✓ |

**更正记录**：我先前一次排查曾判断"打包版没有宿主日志"，那是**查错了目录**（查的是 Chromium userData `%APPDATA%\CowAgent`，那里只有 `updater.log`）。按"先核实再断言"的原则在此更正：**打包版两个日志都正常写入 `~/.cow`**。

## 4. 尚未验证（如实登记）


- ~~真实 NSIS 安装包下的事件复跑~~ → **已完成**（见 §3.1：打包版在 `~/.cow` 写 `desktop-main.log` 与 `run.log`；安装包冒烟见 `release-smoke-windows-installer.md`）。仍缺：磁盘真写满现场、旧超大日志的实操（下两条）。
- 磁盘真正写满（非注入 `ENOSPC`）的现场验证：当前以注入方式覆盖同一代码路径；真实磁盘满场景未执行。
- 旧超大日志的人工处理步骤为文档交付，未在真实 1.1 GB 遗留文件上实操（巡检现场文件已在报告出具时截断）。
