# Windows 桌面端功能检验报告

| 项目 | 值 |
|---|---|
| 检验日期 | 2026-10-13 |
| 分支 / 提交 | `rdai` / `ec0e0d46` |
| 检验对象 | `desktop/`（`cowagent-desktop` 2.2.0，Electron 33.4.11 / Chrome 130.0.6723.191 / Node 20.18.3）|
| 平台 | Windows（`win32`，DPI 125%）|
| 运行形态 | 源码检出 + 开发模式（`electron .`），本机模式（无 `desktop-remote.json`）|
| 检验方式 | Electron `--remote-debugging-port` + CDP 协议巡检、应用自身日志、后端日志、源码/契约核对 |
| 结论 | 应用可用且核心功能正常；**发现 3 项需优先修复的缺陷**（其中 1 项可写满磁盘），以及若干平台能力缺口与文案缺陷 |

---

## 1. 检验环境与启动方式

### 1.1 两个环境卡点（首次启动即遇到）

**（a）`ELECTRON_RUN_AS_NODE` 继承导致 Electron 静默退出**

从 Electron 宿主环境（本次为 DSH 自身）启动时，`ELECTRON_RUN_AS_NODE=1` 被继承，`electron.exe` 退化为纯 Node 运行：

```
TypeError: Cannot read properties of undefined (reading 'registerSchemesAsPrivileged')
    at dist/main/remote/remote-container-ipc.js:139
Node.js v20.18.3            ← 打印的是 Node 版本，而非 Electron 版本
```

表现为**退出码 0、无任何输出**，极易误判为"没问题"。清除该变量后 `electron.exe --version` 恢复为 `v33.4.11`。普通用户双击启动不受影响。

**（b）本地后端找不到可用 Python（`code 9009`）**

```
[MAIN][2026-10-13T02:06:50.670Z] [ERROR] [backend] error: backend_crashed — The app exited during startup (code 9009)
```

`9009` 为 Windows「找不到命令」。原因链见 §4.2。本机可用解释器为 micromamba 的 `py312` 环境。

### 1.2 实际使用的启动命令

```powershell
# 在 desktop/ 目录下
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
$env:PATH = 'D:\tools\micromamba\envs\py312;D:\tools\micromamba\envs\py312\Library\bin;D:\tools\micromamba\envs\py312\Scripts;' + $env:PATH
# 关键：stdout 必须重定向到文件，否则触发 §4.1 的 EPIPE 死循环
Start-Process -FilePath '.\node_modules\electron\dist\electron.exe' `
  -ArgumentList '.','--remote-debugging-port=9222' `
  -WorkingDirectory (Get-Location).Path `
  -RedirectStandardOutput "$env:TEMP\app-stdout.txt" `
  -RedirectStandardError  "$env:TEMP\app-stderr.txt"
```

### 1.3 数据目录（开发模式）

| 路径 | 用途 | 本机实测 |
|---|---|---|
| 仓库根 `config.json` | 后端配置 | 2,371 字节 |
| 仓库根 `identity.db` | 身份/设备/绑定/租约 | 60,653,568 字节 |
| 仓库根 `run.log` | **后端**（Python）日志 | 约 298 KB |
| `~/.cow/run.log` | **主进程**（Electron）日志镜像 | 曾失控至 1.1 GB，见 §4.1 |
| `%APPDATA%\CowAgent\` | Electron userData | `window-state.json`、`Preferences`、`Local State` |

---

## 2. 结果总览

| 类别 | 通过 | 问题 |
|---|---|---|
| 启动链路 | 3 | 2（1 个致命） |
| OS 集成 | 7 | 1（待查） |
| 桌面客户端 | 7 | 1 |
| 管理控制台 | 15 个页面 | 1（403 故障） |
| 平台能力 | — | 2（设计性缺口 + 文案误导） |
| 运行环境 | — | 1（缺依赖） |

---

## 3. 通过项明细

### 3.1 启动与环境

| 项 | 实测证据 |
|---|---|
| Electron 主进程启动 | CDP `/json/version` → `RongAI/2.2.0 Chrome/130.0.6723.191 Electron/33.4.11` |
| 本地 Python 后端 | `[backend] ready on port 9899`；`GET /api/health` → `{"status":"ok"}`（PID 30888 = `python app.py`）|
| 本机 Web 控制台 | `GET http://127.0.0.1:9899/` → 200，325,755 字节 |
| 单实例锁 | [index.ts:787](../../desktop/src/main/index.ts) `requestSingleInstanceLock()`；实测启动第二实例后只剩一个主进程 |
| 窗口状态记忆 | `%APPDATA%\CowAgent\window-state.json` = `{"x":128,"y":8,"width":1368,"height":800}` |
| 关闭到托盘 | [index.ts:456](../../desktop/src/main/index.ts) `shouldCloseToTray`，Windows 默认驻留 |
| 开机自启 | [index.ts:663](../../desktop/src/main/index.ts) Windows 用 `--hidden` 参数 + `wasOpenedAsHidden` 判定 |
| 系统托盘 | [index.ts:861](../../desktop/src/main/index.ts) 仅非 macOS 创建；图标解析到 `desktop/resources/icon.png`（存在）|
| Windows 通知 | [index.ts:131](../../desktop/src/main/index.ts) `setAppUserModelId`；[:760](../../desktop/src/main/index.ts) `new Notification` |
| Windows 快捷方式修复 | [index.ts:875](../../desktop/src/main/index.ts) `repairWindowsShortcuts()`（仅打包版有意义）|
| 自动更新 | 开发模式正确跳过：`not packaged — updater wiring skipped` |

### 3.2 桌面客户端（登录后：`admin` @ 默认租户）

| 项 | 实测证据 |
|---|---|
| 浏览器授权登录 | 走回环监听 + 系统浏览器；密码只在浏览器输入，桌面端不经手（[LoginGate.tsx:45](../../desktop/src/renderer/src/components/LoginGate.tsx)）|
| 导航 | 对话 / 团队 / 知识 / 记忆 / 能力 / 通道 / 定时 / 设置 —— 全部可达 |
| **聊天与流式回复** | 发送「只回复两个字：收到」→ 收到回复「收到」；历史会话正常归档（今天 / 更早）|
| 设置页 | 模型配置（DeepSeek / `deepseek-v4-flash`）、Agent 配置（最大上下文 Token、最大记忆轮次、最大执行步数、深度思考、子 Agent、自主进化）、安全设置、语言 / 任务通知 / 通知声音 / 开机自启 / 远程服务器 |
| **深色模式** | 切换后 `documentElement.className` = `cow-remote-covering platform-win dark`；状态持久化于 `localStorage.cow_theme` |
| 更多菜单 | 技能广场 / 文档中心 / 官网 / 反馈 / 检查更新 / 深色模式 / 主题 / 语言 EN / 日志 / v2.2.0 |
| 工作空间面板 | 可打开（预览 / 文件 页签；当前「空目录」）|
| 平台标识 | 页面根元素带 `platform-win` 类，前端可感知平台 |

### 3.3 管理控制台（第二窗口，`http://localhost:9899`）

15 个页面全部可达，其中 **11 个页面 0 控制台报错、0 失败请求**：

控制台概览（智能体 1 / 今日消息 0 / 成员 2 / 系统状态 正常运行）、智能体管理、能力中心（**21 个内置工具**）、模型服务、消息渠道、系统接入、成员管理（`admin` 租户管理员 + `e2e_member` 成员）、组织架构、角色权限、租户管理、品牌设置、审计日志、Token 消耗、运行日志（**实时日志流 + DEBUG/INFO/WARNING/ERROR/CRITICAL 过滤**）。

### 3.4 重点验证：MCP 编辑器改动（提交 `d5deb001`）在 Windows 上完全可用

对 `align-mcp-editor-with-master` 这次改动的运行时验证结果：

| 检查点 | 结果 |
|---|---|
| 居中弹窗形态 | ✅ 标题「添加 MCP 工具」，非旧抽屉 |
| 表单 / JSON 双页签 | ✅ 均存在，`window.EcMcpEditor` 已加载（`mcp-*` 元素 id 齐全）|
| 三种传输 | ✅ 自定义下拉（默认 `stdio`）|
| 高级设置 | ✅ 含**工具名前缀**、**超时（秒）**、**暂时停用** |
| 测试连接 / 取消 / 保存 | ✅ |
| **表单 ↔ JSON 往返** | ✅ 写入 `{"mcpServers":{"demo":{"type":"http","url":"https://example.com/mcp","timeout":30}}}` 后切回表单，名称 / url / 超时全部正确回填 |
| **脏草稿守卫** | ✅ 点「取消」弹出「放弃未保存的修改？/ 继续编辑 / 放弃修改」|

> 检验过程**未保存任何数据**，连接数始终为 0。

---

## 4. 问题清单

### 4.1 【P0】`run.log` 失控增长至 1.1 GB

**严重度：高**（可写满用户系统盘）

#### 实测数据

| 时刻 | `~/.cow/run.log` 大小 |
|---|---|
| 首次观测 | 771 MB |
| 6 秒后 | 793 MB（+21.5 MB / 6s ≈ **3.6 MB/s**）|
| 强制停机时 | **1,136.8 MB** |

日志内容为同一异常无限刷屏：

```
[ERROR] [crash] uncaughtException: Error: EPIPE: broken pipe, write
    at Writable.write (node:internal/streams/writable:510:4)
    at console.value (node:internal/console/constructor:303:16)
    at console.<computed> [as error] (dist/main/index.js:65:13)
    at process.<anonymous> (dist/main/index.js:81:17)
```

风暴起点：`2026-10-13T02:11:30.776Z`（紧跟 `[backend] using port 9899` 之后）。

#### 根因（自我喂养死循环）

1. [index.ts:71-74](../../desktop/src/main/index.ts) 包装的 console 先写 run.log，**再调 `original(...args)` 写 stdout**；
2. 写 run.log 的一半有保护 —— [index.ts:60-64](../../desktop/src/main/index.ts) `try { … } catch { /* logging must never break the app */ }`；
3. **写 stdout 的一半没有任何保护**；
4. [index.ts:89-91](../../desktop/src/main/index.ts) 的 `uncaughtException` 处理器又调用 `console.error` → 再次写已断开的 stdout → 再次抛 EPIPE → 再次进入处理器 → **无限循环**，且每轮都向 run.log 追加一次。

#### 触发条件

在 Windows 上非常普通：GUI 程序从控制台启动、父 shell 退出、stdout 管道断开。本次即以「后台 shell 启动 → 包装 shell 退出」复现。

#### 影响

日志文件不封顶增长。本次因 C 盘剩余 75 GB 未触发故障，但长期驻留的实例足以写满磁盘，且期间应用处于异常刷屏状态。

#### 复现步骤

```powershell
# 1. 用会立刻退出的父进程启动（管道必然断开）
Start-Job { & electron.exe . }   # 或任何启动后即退出的包装 shell
# 2. 观察 ~/.cow/run.log 体积
while ($true) { "{0:N0} MB" -f ((Get-Item "$env:USERPROFILE\.cow\run.log").Length/1MB); Start-Sleep 5 }
```

#### 建议修复

给 [index.ts:73](../../desktop/src/main/index.ts) 的 stdout 调用加 try/catch（或写入前判断 `process.stdout.writable`），并让 `uncaughtException` 处理器走**只写文件、不写 stdout** 的直写通道。约 10 行改动即可根除。

---

### 4.2 【P0】找不到 Python 时零诊断（`code 9009`）

**严重度：高**（用户完全无法自救）

界面只有：

> 正在启动 Agent...
> 启动比平时慢，仍在重试中...

**原因链：**

1. [python-manager.ts:419-434](../../desktop/src/main/python-manager.ts) 的 `findPython()` 依次查找：
   - `{backendPath}/.venv/bin/python`
   - `{backendPath}/.venv/Scripts/python.exe`
   - `{backendPath}/venv/bin/python`
   - `{backendPath}/venv/Scripts/python.exe`
2. 本 checkout 的 `.venv` 是**用 uv 在 macOS arm64 上创建的**（`pyvenv.cfg`: `home = /Users/jiantan/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin`，目录内只有 `bin/`，并残留 `.DS_Store`）→ Windows 上永远不可能命中；
3. 于是回退到 [python-manager.ts:433](../../desktop/src/main/python-manager.ts) —— 而本机 PATH 上的 `python` 是 **Microsoft Store 占位别名**（`WindowsApps\python.exe`，运行即提示"请从商店安装"）→ spawn 失败 → `9009`。

**说明：** 开发模式（`app.isPackaged === false`）下后端路径解析到仓库根（[backend-path.ts:24](../../desktop/src/main/backend-path.ts)），以 `python app.py` 启动，因此完全依赖 PATH 上的解释器。

**建议修复：**

- Windows 上跳过 POSIX venv 路径（当前先查 `.venv/bin/python`，在 Windows 上只会误导）；
- 校验候选解释器**真的可执行**（如 `python -c "import sys"`），识别并明确提示 Store 占位别名；
- 自动搜索 micromamba / conda / uv 环境，并支持 `config.json` 中显式配置 `python_path`；
- 启动页直接显示失败原因，而非无限"仍在重试中"。

---

### 4.3 【P1】「记忆管理」100% 不可用（403）

**严重度：中高**（功能完全不可用，且提示无法指引用户）

页面稳定显示：

> 个人记忆目录被替换
> 请点击刷新重试；登录失效时请重新登录。

点「刷新」无法恢复。网络层证据：

```
http.403  http://localhost:9899/api/memory?page=1&page_size=10&category=memory&scope=personal
log.error network: Failed to load resource: the server responded with a status of 403 (Forbidden)
```

（另有一次 `net::ERR_ABORTED Fetch`。其余 14 个管理页面均无网络错误。）

**建议：** 查清"个人记忆目录被替换"这一守卫在开发模式 / Windows 下为何恒触发；能自愈则自愈，不能则给出可操作提示（当前提示指向"重新登录"，而重新登录无效）。

---

### 4.4 【P1】本机目录访问：Windows 上是死路，但 UI 引导用户反复重登

**严重度：中高**（误导性指引 + 平台能力缺口叠加）

Web 控制台在 `/chat` 明确提示，并提供「重新检查 / 登录 / 重新登录」：

> 桌面登录连接已失效，请重新登录以恢复本地目录访问。

但 Windows 上本机目录访问被**平台级禁用**：

| 证据 | 内容 |
|---|---|
| [sandbox.ts:173-181](../../desktop/src/main/local-execution/sandbox.ts) | `win32` 直接返回 `unsupported_platform`，理由为"Windows has no probed execution boundary in this build" |
| [generated-contract.ts:205-214](../../desktop/src/main/project-execution/generated-contract.ts) | `PLATFORM_SUPPORT = { posix: true, win32: false }`；`PLATFORM_TOOLS = { posix: […6 个工具], win32: [] }`（**Windows 一个工具都不给**）|
| [contracts/desktop/v2.json:19-23](../../contracts/desktop/v2.json) | `win32.supported = false`，"the Windows launcher and probes are not accepted yet" |
| [contracts/desktop/v2.json:32-40](../../contracts/desktop/v2.json) | `platform_syntax.windows.bash` = unsupported，且要求"never a translation into POSIX syntax" |
| [capability_matrix.py:678-697](../../auth/capability_matrix.py) | `desktop_project_execution` 保持 `accepted = False` |
| [合并后的 proposal.md](../../openspec/changes/harden-windows-desktop-reliability/proposal.md) | 原 accept-desktop-local-project-execution 已并入；本期发布范围仍明确**保持 Windows 不启用**，不得以声明放宽冒充平台已支持 |

底层原因：本机文件 helper `fs-guard` 是**纯 POSIX 实现**（`libc::openat`、`fdopendir`、`readdir`、`fstatat`、`std::os::unix`），在 Windows 上无法编译（详见迁移后的 `openspec/changes/harden-windows-desktop-reliability/evidence/local-project-execution/local-read-windows.md`）。

**补充实测：** 桌面客户端自身 DOM 中**不存在**「选择本机 / 选择目录」入口（能力未开放）；该流程位于 Web 控制台的 `workspace.js`。

**建议：** Windows 上不要提示"重新登录以恢复"，应直接说明「当前平台不支持本机文件访问」；在平台能力补齐前隐藏相关入口。

---

### 4.5 【P1】错误文案把「平台不支持」误报为「未连接」

[workspace.js:514](../../channel/web/static/js/workspace.js) 将 `device_error`（本机执行失败）与 `device_offline`（确实未连接）折叠为同一文案：

```js
if (code === 'device_offline' || code === 'device_error') return t('ws_local_offline');
if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');
```

于是平台能力缺失（helper 不可用）被呈现为连接故障，用户按提示"重新登录"永远无法解决。

**建议：** 为 `platform_unsupported` / helper 缺失引入独立错误码与文案，与连接类错误彻底分离。

---

### 4.6 【P2】其余问题

| # | 问题 | 说明 |
|---|---|---|
| 1 | 本地环境缺依赖 | micromamba `py312` 中 **`playwright`、`tenacity` 均缺失**（`desktop/build/requirements-desktop.txt` 两者都列出），实测 `ModuleNotFoundError`。后端启动不受影响，但**浏览器工具**与 dashscope 部分重试路径会在运行时失败 |
| 2 | `ELECTRON_RUN_AS_NODE` 陷阱 | 从 Electron 宿主终端启动时应用静默退出（退出码 0、无输出），报错仅在 stderr 可见 |
| 3 | 第二个后端进程抢端口 | 观察到第二个 `app.py` 启动并被 `WinError 10048` 拒绝（`run.log:2089`）。拒绝路径与错误信息均正确，但**"为何发起第二次 spawn"未查到底** |
| 4 | 命名窗口导航不一致 | 「控制台」为 `<a href="/admin" target="cow-admin">`、「工作台」为 `<a href="/chat" target="cow-workbench">`，以命名窗口另开窗口而非应用内导航；且控制台窗口最终落在 `/scheduler` 而非 `/admin` |
| 5 | 截图接口偶发挂起 | CDP `Page.captureScreenshot` 在本次巡检中两次无响应（>60s 不返回），需调用方自备超时 |

---

## 5. 平台能力边界（Windows vs POSIX）

| 能力 | POSIX（macOS） | Windows | 依据 |
|---|---|---|---|
| 客户端安装 / 签名 / 更新 | ✅ | ✅（NSIS，**仅 x64**）| `desktop/package.json`、[electron-builder.win.js](../../desktop/electron-builder.win.js) |
| 聊天 + 远程工作台 | ✅ | ✅ | 本次实测 |
| SAP 页面读取 | ✅ | ✅（`darwin \|\| win32`）；**真实 Windows + SAP 现场验收仍待补齐** | [sap-page-read.ts:16](../../desktop/src/main/remote/sap-page-read.ts)、[desktop-sap-page-reading.md:50](../desktop-sap-page-reading.md) |
| SAP 登录记忆 | ✅ 钥匙串 | ✅ DPAPI | [desktop-sap-login-memory.md:5](../desktop-sap-login-memory.md) |
| **本机项目读写 / 执行** | 代码路径开放（**亦未验收**）| ❌ 明确禁用 | §4.4 |
| ripgrep 快路径 | — | ✅ 专项随包分发 `rg.exe` | [electron-builder.win.js:128](../../desktop/electron-builder.win.js) |

---

## 6. 未检验项及原因

| 项 | 原因 |
|---|---|
| SAP 页面读取 | 需要真实 SAP 系统与 `desktop_sap_page_read_enabled` 开关 |
| Word / Excel / PPT 预览 | 会话工作区为空目录，无可用文件 |
| 工具调用执行 | **主动不测**：会允许模型在本机执行命令 |
| 任务通知 / 通知声音 | 需要长时间后台任务触发 |
| 自动更新 | 开发模式不适用（`not packaged`）|
| 打包安装包行为 | 本次为源码检出运行，未构建 NSIS 安装包 |

---

## 7. 优化建议

### P0 —— 建议立即修复

1. **给 stdout 写入加保护 + 全局吸收 EPIPE**
   把 [index.ts:73](../../desktop/src/main/index.ts) 的 `original(...args)` 包进 try/catch（或写前判 `process.stdout.writable`）；让 `uncaughtException` 处理器只写文件、不碰 stdout。同时监听 `process.on('EPIPE', …)` 兜底。**此举直接消除 1.1 GB 事故。**
2. **`run.log` 增加体积上限与轮转**（例如 10 MB × 1 个备份），并在「运行日志」页面显示文件大小与轮转状态。
3. **Python 探测给出真实诊断**（见 §4.2 建议），特别是：Windows 跳过 POSIX venv 路径、校验解释器可执行、识别 Store 占位别名、支持 `config.json` 指定 `python_path`。

### P1

4. **拆分错误语义**：新增 `platform_unsupported` 独立文案（「当前平台不支持本机文件访问」），把 `device_error` 与 `device_offline` 彻底分开；Windows 上移除「重新登录以恢复本地目录」这条死路提示。
5. **启动时校验依赖**：参照 [check-skill-dependencies.py](../../desktop/build/check-skill-dependencies.py) 为桌面后端增加启动自检，缺失模块直接列名并明确失败，避免"看似可用、调用时才炸"。
6. **修复「记忆管理」403**：定位"个人记忆目录被替换"守卫的触发条件，并在开发模式 / Windows 下给出正确行为。

### P2

7. **Windows 本机执行补齐路径**（若需平台对齐）：重写 `fs-guard` 的 Windows 实现（或改用 Job Object / AppContainer 建立边界）→ 补 launcher 与探针 → 才可把 `contracts/desktop/v2.json` 的 `win32.supported` 置为 `true`。**在此之前，隐藏入口比暴露后报错更诚实。**
8. **后端启动收敛**：同一应用实例只允许一次 spawn，避免第二次尝试撞 `WinError 10048`。
9. **命名窗口路由一致性**：修正 `/admin` → `/scheduler` 的落地差异，或明确"另开窗口"的交互模型。
10. **开发环境脚本**：补 `desktop/build/setup-windows.ps1`（创建 venv + 安装 `requirements-desktop.txt`），避免后续开发者再踩 macOS `.venv` 这个坑。

---

## 8. 附录

### A. 证据文件

| 文件 | 内容 |
|---|---|
| `%TEMP%\cow-desktop-probe\02-login.png` | 启动后的登录页（本机模式，默认 `http://localhost:9899`）|
| `%TEMP%\cow-desktop-probe\admin\10-mcp-editor-form.png` | MCP 编辑器（表单态）——§3.4 证据 |
| `%TEMP%\cow-desktop-probe\admin\11-mcp-editor-json.png` | MCP 编辑器（JSON 态）|
| `%TEMP%\cow-desktop-probe\runlog-head.txt` | `run.log` 失控前 80 行历史（含风暴起点）|
| `%TEMP%\cow-desktop-probe\app-stderr.txt` | Electron 主进程 stderr |
| `%TEMP%\cow-desktop-probe\admin2.json` | 管理控制台 10 页巡检结果（含 403 证据）|

### B. 关键复现命令

```powershell
# 后端健康
Invoke-RestMethod http://127.0.0.1:9899/api/health

# 单实例验证：启动第二个实例后应只剩一个主进程
Get-CimInstance Win32_Process -Filter "Name='electron.exe'" |
  Where-Object { $_.CommandLine -notlike '*--type=*' } | Select-Object ProcessId, CommandLine

# 记忆管理 403
Invoke-RestMethod 'http://127.0.0.1:9899/api/memory?page=1&page_size=10&category=memory&scope=personal'

# 日志体积监控
"{0:N0} MB" -f ((Get-Item "$env:USERPROFILE\.cow\run.log").Length/1MB)
```

### C. 检验方法说明

通过 `--remote-debugging-port=9222` 附加 CDP，使用自建的最小客户端（全局 `WebSocket`，无第三方依赖）执行：

- `Runtime.evaluate` 遍历导航、读取可见文本、驱动表单交互；
- `Runtime.consoleAPICalled` / `Runtime.exceptionThrown` / `Log.entryAdded` / `Network.responseReceived` 收集控制台报错、异常与 HTTP ≥400；
- `Page.captureScreenshot` 留存界面证据。

该方式使检验落在**真实运行的界面与真实网络请求**上，而非仅静态读码。

### D. 环境速查

| 项 | 值 |
|---|---|
| 可用 Python | `D:\tools\micromamba\envs\py312\python.exe`（3.12.13, conda-forge）|
| PATH 上的 `python` | `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`（**Store 占位别名**）|
| 仓库 `.venv` | macOS arm64 / uv / Python 3.14.3（**Windows 上不可用**）|
| Electron | `desktop/node_modules/electron/dist/electron.exe`（33.4.11）|
| 后端进程 | `python D:\AIPJ\rsmagent\app.py`（PID 30888，监听 9899）|

---

*本报告基于 2026-10-13 的一次完整检验会话。#4.1 失控日志已截断为 0 并保留风暴前历史；检验期间未对应用配置或业务数据做任何写入。*
