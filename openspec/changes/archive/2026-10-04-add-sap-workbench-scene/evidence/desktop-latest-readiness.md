# 桌面最新代码加载准备与实际重载

后续最新补记：F4 窄范围补丁及 3.2 复核完成后，原 Electron 将桌面后端 58386 恢复为 61515，Web 58422 沿原环境恢复为 61556；health/静态/匿名拒绝 18 项通过，原环境逐项私有比较相等。Chrome 入口和配置显示通过，没有创建新 runtime；桌面仍为正常浏览器登录入口。当前 859 pass / 1 skip、41/57 及源摘要见 [后续收尾](incremental-closeout.md) / [当前重载记录](incremental-services-reloaded.json)。下述进程号与 819/40 范围保留为上一阶段快照。

最新补记（2026-10-04 00:44:50 +08:00）：原 Electron 29265 已沿既有退出恢复路径将桌面后端 29487 重载为 58386，9876 健康/静态资源正常，数据根、租户根和主密钥私有比较相等。Web 后端也已继承原环境重载为 58422。桌面仍为正常浏览器登录入口，未进行登录后的双栏操作。步骤、Chrome 准备页和安全快照见 [本机收尾](local-closeout.md) / [local-services-reloaded.json](local-services-reloaded.json)。以下保留为重载前准备快照，“本记录未重启”指该准备阶段。

2026-10-04 00:10:48 +08:00，重启前快照。本轮仅检查本机进程元数据、源码和静态文件，以及未登录的 localhost GET；本记录的采集者没有停止或重启服务，没有输入平台/SAP 凭据，没有操作 SAP/OpenCode 页面或调用模型/MCP，没有修改账户、配置或数据库。后续重启结果必须单独记录，不能由本快照推定。

## 进程、数据根与加载缺口

| 服务 | PID / 父 PID | 启动时间（+08:00） | 本机监听 |
| --- | --- | --- | --- |
| 桌面启动器（Node） | 29262 / 1 | 2026-10-03 20:38:59 | 无业务监听 |
| 本项目 Electron 主进程 | 29265 / 29262 | 2026-10-03 20:38:59 | 管理桌面 Python 子进程 |
| 桌面 Python 后端 | 29487 / 29265 | 2026-10-03 20:39:00 | 127.0.0.1:9876 |
| 独立 Web Python 后端 | 54933 / 1 | 2026-10-03 23:56:19 | 127.0.0.1:9899 |

桌面 Electron 执行文件为本项目 `desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron`，工作目录为本项目 `desktop`；后端使用本项目 `.venv/bin/python` 和 `app.py`，工作目录为仓库根。桌面主进程与子进程均沿用 `COW_DATA_DIR=/Users/jiantan/.cow`、`COW_TENANT_BASE=/Users/jiantan/.cow/tenant-roots`；子进程明确使用 `COW_WEB_PORT=9876`、`COW_DESKTOP=1`。Web 独立运行在 9899，不能替代桌面拥有的后端。

在进程内私有读取 macOS `KERN_PROCARGS2` 的 NUL 分隔环境，验证启动器、Electron 和桌面子进程的 `COW_CREDENTIAL_MASTER_KEY` 均存在且相等；没有输出密钥值、指纹或完整环境，没有将环境落盘。采用结构化的 NUL 分隔读取，不能用按空格拆分 `ps eww` 的方式恢复带空格的环境值。此次桌面子进程恢复不需重新注入密钥，由仍存活的原 Electron `process.env` 继承。

下列源码更新晚于旧桌面后端启动时间，独立 Web 后端也早于本轮最新 `http.py` 更新。健康页与静态页不能证明这些 Python 消费路径已重新加载，因此需要沿各自既有启动生命周期重启。

| 文件 | 修改时间（+08:00） | SHA-256 |
| --- | --- | --- |
| `Scene/sap_workbench/backend/identity.py` | 2026-10-03 23:32:57 | `2e9a1dd7fb1500de7d70795717deedfed0b1f612a794da586f2268fa451ad363` |
| `Scene/sap_workbench/backend/runtime.py` | 2026-10-03 23:40:46 | `8191591683ad1e25ab915ec37a8adde679fc4cd6b5ee3f2e54a31e1b871ec13a` |
| `Scene/sap_workbench/backend/http.py` | 2026-10-04 00:06:53 | `ad5a0961990ef5e25e840adece9c1778599f39f3ee104c93867a324541bf6d39` |

Python 部分可能延迟导入，本记录不以修改时间断言每个模块的当前内存版本；它说明进程没有在这些更新后完整启动。源码包含未提交改动，Git HEAD 也不等同于完整交付版本。

## 未登录时可验证的部分

9876 和 9899 的结果一致：`/api/health` 返回 200（仅存活），`/chat` 返回 200（公共页面外壳），SAP 工作台 JS/CSS 返回 200 且与当前源码逐字节哈希一致。`/scene-assets/runtime.js` 为 644,251 字节，与按当前场景清单和共享脚本装配的文件一致，其 SHA-256 为 `aa21fdb56b2296b339795be05ec13ee381386b11b0f7be159356bac3afeba0e9`。共享装配函数有进程级缓存，后续前端修改仍需清除缓存或正常重启，不能仅依赖直接文件 URL。

`/api/scenes/sap-workbench/config` 和 `/api/scenes/sap-workbench/sessions` 在不带租户时均返回 400 / `missing_tenant`；只带 `X-Tenant-ID: default`、不带登录身份时均返回 401 / `unauthorized`。没有以 Web token 或 cookie 访问桌面，没有创建会话。这些结果证明公共资源与未登录拒绝路径可达，不证明受权配置、双栏或工具操作成功。

源静态构建 `/Users/jiantan/ai_assistant/rsmagent/scenes/sap_workbench_assets` 与桌面独立目录 `/Users/jiantan/.cow/scenes/sap_workbench_assets` 本轮重新逐文件核对：两侧均为 1787 个普通文件、84,559,232 字节，相对路径、大小与每个 SHA-256 全相等，`assets_ready` 均通过；未再次复制或覆盖。规范化映射的树哈希仍为 `e5581b5123c2c20aa0b1b38ba0e1c0e9731458490f0879b85869b3e9aa0dada9`，计算方式见 `desktop-assets.md`。

以显式的实际 MCP Python 3.10 路径、桌面 data-root、既有可信项目 `/Users/jiantan/ai_assistant/sapwork` 和 `sap-browser-worker` 重跑离线环境 CLI：退出码 0，`browser`、`opencode_runtime`、`embed`、`project`、`mcp_runtime` 五项通过，`network_tested: false`。没有启动解释器所承载的 MCP、创建目录/数据库或联网探测；不代表实际版本兼容和登录成功。

## 既有桌面生命周期的重启接缝

已只读复核源码 `desktop/src/main/python-manager.ts` 与当前已构建的 `desktop/dist/main/python-manager.js`：两者均实现子进程 `exit` 后在 `wasReady && !shuttingDown` 条件成立时执行 `recover()`，随后 `restart()` 调用 `stop()`、等待 2 秒再 `start()`。`recovering` 防止健康探测与退出回调重复恢复；`start()` 从同一 Electron 的环境和同一后端/数据根启动自己的 Python 子进程。已构建文件 SHA-256 为 `fba65dea93ecd29a9386dbaa0069f1b95d6cd5bd653cd1b7c57836145eeb1435`。

供主代理执行的有限步骤：

1. 在发送信号前重新核对精确 PID、父 PID、执行文件、工作目录、9876 监听所有权及健康就绪；旧 PID 如已变化则重新识别，不按过期记录发信号。
2. 仅向已确认由该 Electron 管理、已经 ready 的旧后端发送一次 SIGTERM，让原 Electron 的退出恢复路径启动新后端。不自行并行启动 Python，不杀其他同名进程，不更改桌面配置或复制 Web 登录状态。
3. 有界等待新子进程，核对新 PID、相同父 Electron、实际监听端口、健康页和静态资源；私有核对新子进程仍继承同一数据根及主密钥，仅记录相等布尔值。如果恢复预算耗尽或启动失败，记录结果，使用既有桌面重试/完整退出流程处理，不无界发信号。
4. Web 后端沿自身原启动环境单独重启，桌面就绪事件继续负责公布桌面端口。桌面菜单 Reload 只刷新 renderer，不能代替后端重启。

这些是源码和构建产物支持的操作路径，本记录没有执行它们，也没有将恢复回调的静态检查冒充实际重启成功。正常完整退出备选路径会触发 `before-quit` 的 `pythonBackend.stop()`；如需重新运行原启动器，应在退出前仅于私有内存保留原结构化环境，保持数据根和密钥，勿把环境打印、写入公开日志或创建孤立后端。当前无需改源码、IPC 或重建桌面。

## 桌面账户与验收边界

桌面使用独立身份库。只读账户状态确认现有 `admin` 活跃，默认租户及成员关系活跃，`must_change_password=1`，临时密码有效期在本次检查时尚未到期；未读密码/散列，未测试或重置密码。登录 UI 当前仍在正常认证入口，浏览器授权超时提示不能作为已登录证明。进入受权工作台必须沿桌面自己的有效凭据完成正常登录与首次改密；不能复制 Web 账户密文、token、cookie 或整个数据库规避这一步。

此记录不完成任务 7.2 的桌面双栏、输入、流式回复、权限交互、恢复及人工接管验收，也不代替 5.7 的真实 SAP 控件、G3 保存与不确定结果核对。资源可达、离线检查及拒绝路径通过应与真实业务操作结果分别保留。
