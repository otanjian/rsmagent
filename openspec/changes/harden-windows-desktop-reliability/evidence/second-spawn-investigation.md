# 阶段 5 证据：第二次 spawn 的来源与端口守卫（任务 5.1）

日期：2026-10-13（Windows x64 / Python 3.12.13 / Node 24.19）。报告遗留问题：`docs/design/windows-desktop-verification-2026-10-13.md` 第 289 行"观察到第二个 `app.py` 启动并被 `WinError 10048` 拒绝……**为何发起第二次 spawn 未查到底**"。

## 1. 结论（分两半）

**一半：本实例内部不存在重复 spawn。** 代码路径：

- `PythonBackend.start()` 开头 `if (this.status === 'ready' || this.status === 'starting') return`（`python-manager.ts:727-730`）；
- 健康检查触发的恢复走 `restart()`，由 `this.recovering` 单飞（`:1050-1057`）；
- `pickPort()` 在 `start()` 内只调用一次（`:776`）。

因此日志里出现两次启动 = **两个不同的进程**，不是管理器重入。按任务要求，**没有做任何"串行器重构"**。

**另一半：第二次确实存在，来源是"桌面端自己的那一次 spawn"与"用户命令行起的后端"撞端口，而它本该被自己的端口守卫避开却没有。** 证据链：

| 证据 | 位置 |
|---|---|
| 失败实例带着桌面模式标记 | `run.log:2084-2088`：`[desktop] plugin GODCMD/KEYWORD/… disabled (not needed in desktop client)` → 这一行是**桌面端启动**的后端 |
| 同一时刻已有别的后端在服务 | `run.log:2089-2091`：`端口 9899 已被占用` + `[WinError 10048]`；紧接着 `run.log:2100` 又出现 `[WebChannel] Slow history list`（另一个进程在正常服务，同一个 run.log） |
| 正在服务的是用户手动起的进程 | 报告第 393 行：`python D:\AIPJ\rsmagent\app.py`（PID 30888，监听 9899）；且它**始终没有被杀**（会话中它一直服务到进程消失） |
| 首选端口来自配置 | `python-manager.ts:497` `readConfiguredPort(dataDir)`；桌面默认端口是 9876，而此机 `config.json` 的 `web_port` 是 9899，所以首选候选 = 9899 |

## 2. 根因：端口探测说谎（已现场复现）

`isPortFree()` 原来只做一次 `bind 127.0.0.1:9899`，把"bind 成功"当作"端口空闲"。**Windows 上普通 winsock bind 默认带 `SO_REUSEADDR`**，因此在另一个进程已经监听 `0.0.0.0:9899` 的情况下，这个 bind 仍然成功 → `pickPort()` 返回 9899 → 后端（cheroot，不共用地址）绑定失败 `WinError 10048`。

现场复现（不需要启动应用，占用 `0.0.0.0:9899` 的真实套接字）：

```
$ node scripts/repro/probe-port-free-windows.cjs
a socket holds 0.0.0.0:9899 (the way the backend serves)
  shipped probe  (bind 127.0.0.1:9899) says free      : true
  backend-port.isPortFree (the fix) says free             : false
  connect test   (dial 127.0.0.1:9899) says occupied  : true
  the old probe was WRONG about this port
RESULT: the fix says the port is busy (correct)
```

**同一句谎话还掩盖了第二个问题**：`freePort()` 只在 `isPortFree()` 为假时才会去清理监听者；由于探测永远说"空闲"，清理分支**从未执行**——这才是用户手动启动的后端能活下来的唯一原因。若只修探测而不修清理，`freePort()` 就会开始杀掉**用户自己的 `python app.py`**：`freePort()` 原来对"任何监听该端口的 PID"发 SIGTERM/SIGKILL，其注释里"我们就不会碰无关应用"的假设，靠的只是"它在监听我们的端口"。这与任务 5.1 明确要求的"**不杀无关进程**"直接冲突。因此两项必须一起改。

## 3. 改动（最小、局部）

| 位置 | 改动 |
|---|---|
| `desktop/src/main/backend-port.ts`（新增） | 纯 Node、可单测的端口规则：`canBind`、`portAnswers`、`isPortFree`（bind **且** 连不上才算空闲）、`listenerPids`、`listenerImage`、`isOurBackend`（只有"本实例 spawn 的子进程"或"随包分发的 `cowagent-backend.exe`"才算自己的） |
| `desktop/src/main/python-manager.ts` | `isPortFree`/`findListenerPids` 改为委托该模块；`freePort()` 两轮（SIGTERM/SIGKILL）都先经过 `isOurBackend` 过滤，非自己的监听者只记录日志并**保留运行**，由 `pickPort()` 继续尝试下一个候选端口 |
| `docs/desktop-startup-and-python.md` | 新增第 7 节"排查：第二个后端进程 / 端口被占用"（现象、两类来源、根因、现行为、排查命令、"不要 taskkill /IM python.exe"） |

未新增任何状态机、快照或串行器；未改动契约与生成文件。

## 4. 验证

`tests/test_desktop_backend_port_guard.cjs`（新增，**6/6 通过**，用真实套接字，测的是编译产物）：

1. 有人监听 `0.0.0.0:<port>` 时 `isPortFree` **为假**——即使 bind 成功（Windows 上额外断言 bind 确实成功，把这个 OS 怪癖钉在测试里）；
2. 没人监听的端口仍为真；
3. `listenerPids` 能找到本进程的监听（PID 命中）；
4. `listenerImage` 能回答真实 PID 的镜像名；
5. `isOurBackend`：自己的子进程 PID → 真；`cowagent-backend.exe` → 真；`python.exe`（含"非自己子进程"）与无法识别的监听者 → **假**；
6. `freePort` 源码里 SIGTERM 与 SIGKILL 两轮都必须经过所有权过滤，且"没有自己的监听者时直接返回"。

其余：

| 检查 | 结果 |
|---|---|
| `tsc -p tsconfig.main.json --noEmit` | **exit 0** |
| `tests/test_desktop_startup_contract.cjs` / `_python_probe` / `_host_log` / `_local_files_bridge` / `_remote_host` | 7 / 17 / 16 / 6 / 34 全通过，0 失败 |
| 端口复现脚本 | 旧探测 WRONG、修复后 busy（见上） |
| 端口释放 | 脚本跑完 `9899` 与 `45999` 均无残留监听（脚本自查） |

## 5. 判定与未验证

- **5.1 勾选**：来源、PID/端口已定；内部竞态不存在（未做串行器重构）；端口守卫的"单有效后端"与"不杀无关进程"由行为测试守住；排查说明已进文档。
- **未验证**：打包应用下的端到端双进程场景（需要真实安装包）：本机验证到"端口规则 + 所有权规则"这一层，未启动真实安装包与用户命令行后端对撞。
