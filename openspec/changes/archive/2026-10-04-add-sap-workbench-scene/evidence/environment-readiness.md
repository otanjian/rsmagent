# 本机运行准备与有界恢复

记录日期：2026-10-03。用户已暂停 OpenCode 与 SAP Web GUI 的实际操作检查；本轮仅执行独立实现、隔离测试及本机离线检查，没有新增真实 SAP 页面、模型或 MCP 操作。

## 离线检查结果

本机 `RuntimePaths` 的 5 个分项实际离线检查均为 `passed`，输出模式为 `offline_local_environment`，`network_tested` 为 `false`，原始安全结果见 [offline-environment.json](offline-environment.json)。检查使用当前本机数据根和已配置项目目录，未启动服务、访问网络或创建目录/数据库。

| 分项 | 实际检查内容 | 结果及边界 |
| --- | --- | --- |
| `browser` | 已登记本机节点及 Chrome 绝对执行文件存在、可执行 | 通过；不代表能启动浏览器或证书可信 |
| `opencode_runtime` | Bun 绝对执行文件存在、可执行；OpenCode 源码目录内所需入口文件及根 `node_modules` 存在 | 通过；不执行模块导入，不代表依赖完整或版本兼容 |
| `embed` | 本机 `index.html` 可读取，包含外部脚本；其 JS/CSS/modulepreload 入口引用解析为资源目录内非空文件 | 通过；拒绝外部地址和越界引用，不递归验证全部构建块或界面渲染 |
| `project` | 当前本机 canonical host 的项目路径为绝对目录，具有读取和进入权限 | 通过；不代表另一台 OpenCode API 主机上的目录可用 |
| `mcp_runtime` | 配置或缺省 MCP Python 绝对执行文件存在、可执行 | 通过；不执行 Python 依赖导入，不代表网关连通或 SAP 登录成功 |

可重复执行的入口为：

```sh
.venv/bin/python -B -m Scene.sap_workbench.backend.environment \
  --data-root /path/to/platform-data \
  --project /path/to/sap-project \
  --browser-ref sap-browser-worker
```

全部通过退出码为 0；存在失败项退出码为 1。输出仅包含检查标识、通过/失败、本机环境标记和固定失败原因，不输出凭据。`-B` 禁止写入 Python 字节码缓存；Chrome 探测不导入主应用日志或配置模块。冷启动子进程隔离测试使用未创建的临时路径，确认检查返回失败分项后未创建日志、数据、项目或 runtime 目录。MCP Python 对画面会话为按需依赖，离线命令仍完整报告该分项。

## 节点、执行目录与配置修复

离线检查和专属浏览器 runner 共用 Chrome 选择函数。显式 `SAP_WORKBENCH_CHROME` 无效时不会悄悄改用另一个 Chrome，以免检查对象和实际执行对象不一致。

本机节点登记值为 `sap-browser-worker`，已有 `local` 别名继续可用。配置表单改为下拉选择；新配置保存和会话创建拒绝未知节点。旧配置仍可读取并进入配置页修复，能力状态显示其节点不可用。

当前 canonical host 和主后端在同一机器执行，目录检查明确标记为 `environment: local`。OpenCode API 健康检测不替代项目目录检查，本机目录通过不用于推断远程目录。远程执行节点与远程显示未在本轮实现或验收。

## 超时与恢复边界

| 环节 | 时限 | 失败处理 |
| --- | --- | --- |
| SAP HTTPS 与 OpenCode API 健康检测 | 各 10 秒 | 显示分项失败；检测不创建会话或调用模型 |
| MCP 探测初始化 | 50 秒 | 返回固定 `mcp_check_timeout`，进入 worker 清理 |
| MCP worker 关闭 | 正常关闭最多 12 秒；强制结束后等待最多 2 秒 | 处理管道 drain 或进程退出停滞；保留取消传播 |
| MCP 连接清理 | 两个 disconnect 并行，各 5 秒；随后连接栈关闭 5 秒 | 连接栈在进入它的同一任务退出，避免跨任务退出 AnyIO cancel scope |
| 完整 host 启动 | 70 秒 | 回收启动资源，保留绑定和启动阶段，记录固定 `opencode_start_timeout` 后可重试 |

联网探测的两个 HTTP 检测、MCP 初始化和 worker 清理总预算最多 84 秒，位于 HTTP 请求 90 秒预算内。失败原因使用登记错误码，不保存或回传网关原始异常文本。主后端本轮未重启，新增补丁在下次重启后加载。

MCP 连接栈使用同任务 deadline，兼容实际 worker 的 Python 3.10，不新增 `async_timeout` 依赖。实际 worker Python 3.10.0 的离线 AnyIO 检查结果见 [worker-compatibility.json](worker-compatibility.json)：两个 disconnect、两个同任务退出、慢退出触发 deadline 以及超时后继续执行均通过。该检查使用纯内存任务组，不访问真实 MCP 网关、SAP 或模型。

网关 `submit` 等待超时会取消外层请求，避免无人等待的请求继续执行；已登记且被 `shield` 保护的分配和清理仍由 runtime 管理，重试可核对原绑定继续恢复。隔离测试同时验证外部取消保留原语义、嵌套 deadline 不误取消其他范围及网关超时取消请求。

## 验证和交付范围

离线环境、配置节点修复、前端下拉/错误提示、探测超时、worker 清理与 host 超时使用隔离测试验证。此处保留上一轮阶段记录：SAP 独立套件 **251 项通过，耗时 49.19 秒**，相关 Node **80 项通过**，MCP 与 deadline 专项另一次执行 **50 项通过**。这些套件存在覆盖重叠，不将数量相加。隔离测试使用临时文件或替身，不作为真实 SAP、模型、MCP 联网结果；Bun 和真实现场未重测。后续补丁及最新完整结果见 [验收矩阵](acceptance-matrix.md)。

任务 3.10 按当前本机同进程节点及 host 消费端范围完成，上一轮阶段进度为 **36/57，剩余 21 项**；后续完成 6.1 后为 **37/57，剩余 20 项**，change 保持未归档。实际操作测试暂停；统一登录、完整生产权限/审批/配额、远程部署和 G3 仍按此前安排延后。原有真实现场记录保持原测试时间及原验证边界，不作为本轮新增代码的重测证据。扩展回归和历史资源摘要失败边界见 [验收矩阵](acceptance-matrix.md)。

## 缺失服务恢复

并行运行准备核对发现 OpenCode API/Web 已退出；按用户既有启动授权从未修改的 `rsmcode/opencode` 恢复两个 loopback 服务，后台进程已脱离终端。只用 `lsof` 与 `ps` 检查监听，没有发送 API 请求或执行对话、浏览器、SAP/MCP 工具操作。

| 服务 | 监听 | 恢复后的 PID |
| --- | --- | --- |
| OpenCode API | `127.0.0.1:4096` | Bun `40462` |
| OpenCode Web | `127.0.0.1:3000` | Node `40466`，Bun 启动器 `40465` |

API 在 `packages/opencode` 执行 `bun run ./src/index.ts serve --hostname 127.0.0.1 --port 4096 --no-mdns`；Web 在 `packages/app` 执行 `bun run dev -- --host 127.0.0.1 --port 3000 --strictPort`。`serve` 启动为 `instance: false`，实例按请求加载，单独恢复监听不会创建 SAP 项目实例或执行 MCP 登录。

原有桌面后端 `9876`、主 Web 后端 `9899`、桌面 Vite `5173`、MCP `8100/8200` 继续使用原进程；主后端新增逻辑仍待下次正常重启加载。`rsmcode/opencode` Git 工作区保持干净，服务监听不作为真实工作台联调通过。
